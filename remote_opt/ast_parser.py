import os

try:
    from tree_sitter import Language, Parser, Query
    import tree_sitter_python as tspython
    HAS_TREESITTER = True
except ImportError:
    HAS_TREESITTER = False

class CodebaseASTParser:
    """
    AST Parser inspired by codebase-memory-mcp to extract code intelligence
    and minimize token usage by returning signatures instead of whole files.
    """
    def __init__(self):
        self.parser = None
        self.lang_python = None
        if HAS_TREESITTER:
            self.parser = Parser()
            # In tree-sitter Python bindings, we can just use the provided Language object
            self.lang_python = Language(tspython.language())
            print("✅ Tree-sitter initialized with Python grammar.")
        else:
            print("⚠️ tree-sitter not installed. AST parsing will fallback to basic extraction.")

    def extract_symbol(self, filepath: str, symbol_name: str) -> str:
        """
        Extract the definition and signature of a specific symbol (function/class)
        from a file, avoiding loading the entire file into the LLM context.
        """
        if not os.path.exists(filepath):
            return f"Error: File {filepath} not found."
            
        with open(filepath, 'r', encoding='utf-8') as f:
            code = f.read()

        # If Tree-sitter is installed and it's a Python file
        if HAS_TREESITTER and self.parser and filepath.endswith('.py'):
            self.parser.language = self.lang_python
            tree = self.parser.parse(bytes(code, "utf8"))
            
            # Write a tree-sitter query to find a class or function by name
            query_str = f"""
            (class_definition name: (identifier) @name (#eq? @name "{symbol_name}")) @definition
            (function_definition name: (identifier) @name (#eq? @name "{symbol_name}")) @definition
            """
            try:
                query = Query(self.lang_python, query_str)
                captures = query.captures(tree.root_node)
                
                # Find the definition capture block
                for node, capture_name in captures:
                    if capture_name == "definition":
                        start_byte = node.start_byte
                        end_byte = node.end_byte
                        return f"--- AST Extraction: {symbol_name} ---\n{code.encode('utf8')[start_byte:end_byte].decode('utf8')}"
                return f"Symbol '{symbol_name}' not found in AST."
            except Exception as e:
                print(f"Tree-sitter extraction failed: {e}. Falling back to basic extraction.")
        
        # Fallback simplistic extraction
        lines = code.split('\n')
        extracted = []
        in_symbol = False
        indent_level = 0
        
        for line in lines:
            if line.strip().startswith(f"def {symbol_name}(") or line.strip().startswith(f"class {symbol_name}"):
                in_symbol = True
                indent_level = len(line) - len(line.lstrip())
                extracted.append(line)
                continue
                
            if in_symbol:
                current_indent = len(line) - len(line.lstrip())
                if line.strip() != "" and current_indent <= indent_level:
                    break # End of symbol block
                extracted.append(line)
                
        if extracted:
            return "\n".join(extracted)
        return f"Symbol '{symbol_name}' not found."

if __name__ == "__main__":
    ast = CodebaseASTParser()
    # Basic self-test
    print(ast.extract_symbol(__file__, "CodebaseASTParser"))
