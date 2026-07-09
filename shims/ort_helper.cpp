#include <iostream>
#include <vector>
#include <string>
#include <cmath>
#include <algorithm>
#include "onnxruntime_cxx_api.h"

// Struct to hold ONNX Runtime session variables
struct OrtSessionState {
    Ort::Env env;
    Ort::Session session;
    std::vector<std::string> input_names;
    std::vector<std::string> output_names;

    OrtSessionState(const char* model_path) 
        : env(ORT_LOGGING_LEVEL_WARNING, "mojomem_ort"),
          session(nullptr) 
    {
        Ort::SessionOptions session_options;
        session_options.SetIntraOpNumThreads(1);
        session_options.SetGraphOptimizationLevel(GraphOptimizationLevel::ORT_ENABLE_ALL);
        
#ifdef _WIN32
        // Convert model_path (UTF-8) to wstring on Windows
        int len = MultiByteToWideChar(CP_UTF8, 0, model_path, -1, NULL, 0);
        std::wstring wmodel_path(len, 0);
        MultiByteToWideChar(CP_UTF8, 0, model_path, -1, &wmodel_path[0], len);
        // Remove trailing null-char if added
        if (!wmodel_path.empty() && wmodel_path.back() == L'\0') {
            wmodel_path.pop_back();
        }
        session = Ort::Session(env, wmodel_path.c_str(), session_options);
#else
        session = Ort::Session(env, model_path, session_options);
#endif

        Ort::AllocatorWithDefaultOptions allocator;
        
        // Query inputs
        size_t num_input_nodes = session.GetInputCount();
        for (size_t i = 0; i < num_input_nodes; i++) {
            auto input_name_alloc = session.GetInputNameAllocated(i, allocator);
            input_names.push_back(std::string(input_name_alloc.get()));
        }

        // Query outputs
        size_t num_output_nodes = session.GetOutputCount();
        for (size_t i = 0; i < num_output_nodes; i++) {
            auto output_name_alloc = session.GetOutputNameAllocated(i, allocator);
            output_names.push_back(std::string(output_name_alloc.get()));
        }
    }
};

extern "C" {

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

EXPORT void* init_ort_session(const char* model_path) {
    try {
        auto state = new OrtSessionState(model_path);
        return static_cast<void*>(state);
    } catch (const std::exception& e) {
        std::cerr << "Error initializing ONNX Runtime: " << e.what() << std::endl;
        return nullptr;
    }
}

EXPORT int run_ort_inference(void* session_ptr, 
                             const int64_t* input_ids, 
                             const int64_t* attention_mask, 
                             const int64_t* token_type_ids, 
                             int seq_len, 
                             float* out_embedding) 
{
    if (!session_ptr) return -1;
    
    try {
        auto state = static_cast<OrtSessionState*>(session_ptr);
        auto& session = state->session;
        
        Ort::MemoryInfo memory_info = Ort::MemoryInfo::CreateCpu(OrtDeviceAllocator, OrtMemTypeDefault);
        std::vector<int64_t> input_shape = {1, seq_len};
        size_t total_elements = seq_len;
        
        std::vector<Ort::Value> input_tensors;
        std::vector<const char*> active_input_names;
        
        // 1. Prepare input_ids
        input_tensors.push_back(Ort::Value::CreateTensor<int64_t>(
            memory_info, const_cast<int64_t*>(input_ids), total_elements, input_shape.data(), input_shape.size()));
        active_input_names.push_back("input_ids");
        
        // 2. Prepare attention_mask
        input_tensors.push_back(Ort::Value::CreateTensor<int64_t>(
            memory_info, const_cast<int64_t*>(attention_mask), total_elements, input_shape.data(), input_shape.size()));
        active_input_names.push_back("attention_mask");
        
        // 3. Prepare token_type_ids (if required by model)
        bool has_token_type_ids = std::find(state->input_names.begin(), state->input_names.end(), "token_type_ids") != state->input_names.end();
        if (has_token_type_ids) {
            if (token_type_ids == nullptr) {
                // If the model requires it but we didn't pass it, generate all zeros
                static std::vector<int64_t> zeros;
                if (zeros.size() < total_elements) {
                    zeros.resize(total_elements, 0);
                }
                input_tensors.push_back(Ort::Value::CreateTensor<int64_t>(
                    memory_info, zeros.data(), total_elements, input_shape.data(), input_shape.size()));
            } else {
                input_tensors.push_back(Ort::Value::CreateTensor<int64_t>(
                    memory_info, const_cast<int64_t*>(token_type_ids), total_elements, input_shape.data(), input_shape.size()));
            }
            active_input_names.push_back("token_type_ids");
        }

        // Determine output target (sentence_embedding is preferred if exists, else last_hidden_state)
        bool has_sentence_embedding = std::find(state->output_names.begin(), state->output_names.end(), "sentence_embedding") != state->output_names.end();
        std::vector<const char*> active_output_names;
        if (has_sentence_embedding) {
            active_output_names.push_back("sentence_embedding");
        } else {
            active_output_names.push_back("last_hidden_state");
        }

        // Run session
        auto output_values = session.Run(
            Ort::RunOptions{nullptr}, 
            active_input_names.data(), 
            input_tensors.data(), 
            input_tensors.size(), 
            active_output_names.data(), 
            active_output_names.size()
        );

        if (output_values.empty()) return -2;

        if (has_sentence_embedding) {
            // Direct copy from sentence_embedding (usually already pooled and normalized)
            float* data = output_values[0].GetTensorMutableData<float>();
            auto shape = output_values[0].GetTensorTypeAndShapeInfo().GetShape();
            int dim = shape[1];
            std::copy(data, data + dim, out_embedding);
        } else {
            // Manual mean pooling over last_hidden_state
            float* last_hidden_data = output_values[0].GetTensorMutableData<float>();
            auto shape = output_values[0].GetTensorTypeAndShapeInfo().GetShape(); // shape: [1, seq_len, hidden_dim]
            int hidden_dim = shape[2];

            std::vector<float> mean_pooled(hidden_dim, 0.0f);
            float count = 0.0f;

            for (int i = 0; i < seq_len; i++) {
                if (attention_mask[i] > 0) {
                    count += 1.0f;
                    for (int j = 0; j < hidden_dim; j++) {
                        mean_pooled[j] += last_hidden_data[i * hidden_dim + j];
                    }
                }
            }

            if (count < 1e-9f) count = 1e-9f;
            
            float norm = 0.0f;
            for (int j = 0; j < hidden_dim; j++) {
                mean_pooled[j] /= count;
                norm += mean_pooled[j] * mean_pooled[j];
            }
            norm = std::sqrt(norm);
            if (norm < 1e-9f) norm = 1e-9f;

            for (int j = 0; j < hidden_dim; j++) {
                out_embedding[j] = mean_pooled[j] / norm;
            }
        }
        
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "Inference error: " << e.what() << std::endl;
        return -3;
    }
}

EXPORT void free_ort_session(void* session_ptr) {
    if (session_ptr) {
        delete static_cast<OrtSessionState*>(session_ptr);
    }
}

}
