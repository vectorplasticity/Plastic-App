import os
import ast
import networkx as nx
import logging
import tempfile
import uuid
import zipfile
import shutil
from flask import Flask, jsonify, render_template, request, Response
from werkzeug.utils import secure_filename
from networkx.readwrite import json_graph

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
SESSION_STORAGE = os.path.join(tempfile.gettempdir(), 'dependaxy_sessions')
os.makedirs(SESSION_STORAGE, exist_ok=True)
ALLOWED_EXTENSIONS = {'zip'}
app = Flask(__name__, template_folder='.')


def _get_effective_root(base_path):
    effective_root = base_path
    while True:
        items = os.listdir(effective_root)
        visible_items = [item for item in items if not item.startswith('.') and not item == '__MACOSX']
        if len(visible_items) == 1 and os.path.isdir(os.path.join(effective_root, visible_items[0])):
            effective_root = os.path.join(effective_root, visible_items[0])
        else:
            break
    return effective_root

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_imports_for_file(file_path):
    imports = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=os.path.basename(file_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names: imports.add(alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module: imports.add(node.module.split('.')[0])
    except Exception as e:
        logging.warning(f"Could not parse {file_path} for imports: {e}")
    return sorted(list(imports))

def build_full_dependency_graph(root_path):
    G = nx.DiGraph()
    project_root_name = os.path.basename(root_path)
    G.add_node(project_root_name, type='folder')
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d != '__pycache__']
        relative_dir_path = os.path.relpath(dirpath, root_path)
        parent_node_id = os.path.join(project_root_name, relative_dir_path).replace('\\', '/') if relative_dir_path != '.' else project_root_name
        for dirname in dirnames:
            dir_node_id = os.path.join(parent_node_id, dirname).replace('\\', '/')
            G.add_node(dir_node_id, type='folder')
        for filename in filenames:
            file_node_id = os.path.join(parent_node_id, filename).replace('\\', '/')
            node_type = 'python_file' if filename.endswith('.py') else 'other_file'
            G.add_node(file_node_id, type=node_type)
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d != '__pycache__']
        relative_dir_path = os.path.relpath(dirpath, root_path)
        parent_node_id = os.path.join(project_root_name, relative_dir_path).replace('\\', '/') if relative_dir_path != '.' else project_root_name
        for dirname in dirnames:
            dir_node_id = os.path.join(parent_node_id, dirname).replace('\\', '/')
            G.add_edge(parent_node_id, dir_node_id)
        for filename in filenames:
            file_node_id = os.path.join(parent_node_id, filename).replace('\\', '/')
            G.add_edge(parent_node_id, file_node_id)
            if filename.endswith('.py'):
                full_path = os.path.join(dirpath, filename)
                imports = get_imports_for_file(full_path)
                for imp in imports:
                    G.add_node(imp, type='dependency')
                    G.add_edge(file_node_id, imp)
    return G

def generate_numbered_report(effective_root, current_path, prefix="", level=0):
    report_lines = []
    try:
        items = sorted([item for item in os.listdir(current_path) if not item.startswith('.')])
    except FileNotFoundError:
        return []
    py_files = [f for f in items if f.endswith('.py')]
    dirs = [d for d in items if os.path.isdir(os.path.join(current_path, d))]
    counter = 1
    for item in py_files + dirs:
        item_prefix = f"{prefix}{counter}."
        full_path = os.path.join(current_path, item)
        indent = "    " * level
        report_lines.append(f"{indent}{item_prefix} {item}")
        if item.endswith('.py'):
            imports = get_imports_for_file(full_path)
            if imports:
                import_counter = 1
                for imp in imports:
                    imp_prefix = f"{item_prefix}{import_counter}."
                    imp_indent = "    " * (level + 1)
                    report_lines.append(f"{imp_indent}{imp_prefix} {imp}")
                    import_counter += 1
        if os.path.isdir(full_path):
            report_lines.extend(generate_numbered_report(effective_root, full_path, f"{item_prefix}", level + 1))
        counter += 1
    return report_lines

def generate_json_report_recursive(effective_root, current_path, prefix="", level=0):
    """Recursively builds a nested dictionary for the JSON report."""
    report_dict = {}
    try:
        items = sorted([item for item in os.listdir(current_path) if not item.startswith('.')])
    except FileNotFoundError:
        return {}
        
    py_files = [f for f in items if f.endswith('.py')]
    dirs = [d for d in items if os.path.isdir(os.path.join(current_path, d))]
    
    counter = 1
    for item in py_files + dirs:
        item_prefix = f"{prefix}{counter}."
        full_path = os.path.join(current_path, item)
        
        entry = {"name": item}

        if item.endswith('.py'):
            imports = get_imports_for_file(full_path)
            if imports:
                import_dict = {}
                import_counter = 1
                for imp in imports:
                    imp_prefix = f"{item_prefix}{import_counter}."
                    import_dict[imp_prefix] = {"name": imp, "type": "import"}
                    import_counter += 1
                entry["imports"] = import_dict

        if os.path.isdir(full_path):
            entry["files"] = generate_json_report_recursive(
                effective_root, full_path, f"{item_prefix}", level + 1
            )
        
        report_dict[item_prefix] = entry
        counter += 1
        
    return report_dict


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_project():
    session_id = str(uuid.uuid4())
    session_dir = os.path.join(SESSION_STORAGE, session_id)
    os.makedirs(session_dir)
    try:
        project_path = session_dir
        if 'zipfile' in request.files:
            file = request.files['zipfile']
            zip_path = os.path.join(session_dir, secure_filename(file.filename))
            file.save(zip_path)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(project_path)
        elif 'files[]' in request.files:
            files = request.files.getlist('files[]')
            if not files or not files[0].filename:
                 raise ValueError("No files selected in folder.")
            for file in files:
                relative_path = file.filename
                if '..' in relative_path.split(os.path.sep): raise ValueError("Invalid path detected.")
                abs_path = os.path.join(project_path, relative_path)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                file.save(abs_path)
        else:
            raise ValueError("No files uploaded.")

        effective_root = _get_effective_root(project_path)
        full_graph = build_full_dependency_graph(effective_root)

        fs_graph = nx.DiGraph()
        for node, data in full_graph.nodes(data=True):
            if data.get('type') != 'dependency':
                fs_graph.add_node(node, **data)
        for u, v, _ in full_graph.edges(data=True):
            if fs_graph.has_node(u) and fs_graph.has_node(v):
                fs_graph.add_edge(u, v)
        
        search_map = {}
        for u, v, _ in full_graph.edges(data=True):
            if full_graph.nodes[v].get('type') == 'dependency':
                dependency_name, file_name = v, u
                if dependency_name not in search_map:
                    search_map[dependency_name] = {'type': 'dependency', 'nodes': []}
                search_map[dependency_name]['nodes'].append(file_name)
        
        for node_id, data in full_graph.nodes(data=True):
            node_type = data.get('type')
            if node_type in ['python_file', 'other_file']:
                basename = os.path.basename(node_id)
                if basename not in search_map:
                    search_map[basename] = {'type': 'file', 'nodes': []}
                search_map[basename]['nodes'].append(node_id)

        return jsonify({
            "sessionId": session_id,
            "fs_graph": json_graph.node_link_data(fs_graph),
            "search_map": search_map
        })
    except Exception as e:
        logging.error(f"Upload failed: {e}", exc_info=True)
        if os.path.exists(session_dir): shutil.rmtree(session_dir)
        return jsonify({"error": str(e)}), 500

@app.route('/api/analyze', methods=['POST'])
def analyze_node():
    data = request.json
    session_id = data.get('sessionId')
    file_path_id = data.get('filePath')
    session_dir = os.path.join(SESSION_STORAGE, session_id)
    if not os.path.exists(session_dir):
        return jsonify({"error": "Session not found or expired."}), 404
    effective_root = _get_effective_root(session_dir)
    full_graph = build_full_dependency_graph(effective_root)
    snippet_graph = nx.DiGraph()
    if not full_graph.has_node(file_path_id):
        return jsonify({"error": "File node not found in analysis."}), 404
    snippet_graph.add_node(file_path_id, **full_graph.nodes[file_path_id])
    for neighbor in full_graph.neighbors(file_path_id):
        if full_graph.nodes[neighbor].get('type') == 'dependency':
            snippet_graph.add_node(neighbor, **full_graph.nodes[neighbor])
            snippet_graph.add_edge(file_path_id, neighbor)
    return jsonify({"graph": json_graph.node_link_data(snippet_graph)})

@app.route('/api/serialize', methods=['POST'])
def get_serialization_report():
    session_id = request.json.get('sessionId')
    session_dir = os.path.join(SESSION_STORAGE, session_id)
    if not os.path.exists(session_dir):
        return Response("Session not found or expired.", status=404)
    effective_root = _get_effective_root(session_dir)
    report_content = "\n".join(generate_numbered_report(effective_root, effective_root))
    return Response(report_content, mimetype='text/plain')

@app.route('/api/json_report', methods=['POST'])
def get_json_report():
    session_id = request.json.get('sessionId')
    session_dir = os.path.join(SESSION_STORAGE, session_id)
    if not os.path.exists(session_dir):
        return jsonify({"error": "Session not found or expired."}), 404
        
    effective_root = _get_effective_root(session_dir)
    report_json = generate_json_report_recursive(effective_root, effective_root)
    
    return jsonify(report_json)

def main():
    app.run(debug=True, port=5000)
    
if __name__ == '__main__':
    main()
