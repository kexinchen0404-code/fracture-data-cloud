"""Small MCP bridge to one Turso database. Data stays remote; credentials use Windows DPAPI."""
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

URL = "https://research-catalog-kexinchen0404-code.aws-us-west-2.turso.io/v2/pipeline"
SECRET = Path.home() / ".codex" / "research-data" / "token.xml"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Database redirected; credential was not forwarded.")


def load_token():
    if os.name != "nt" or not SECRET.is_file():
        raise RuntimeError("Database authentication is not configured. Run Save-Token.ps1 privately.")
    literal = str(SECRET).replace("'", "''")
    command = "$s=Import-Clixml -LiteralPath '" + literal + "'; [System.Net.NetworkCredential]::new('', $s).Password"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                            capture_output=True, text=True, timeout=15)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Cannot unlock this Windows user's encrypted database credential.")
    return result.stdout.strip()


def execute(sql, args=()):
    stmt = {"sql": sql, "args": [
        {"type": "null"} if value is None else
        {"type": "integer", "value": str(value)} if isinstance(value, int) else
        {"type": "text", "value": str(value)} for value in args], "want_rows": True}
    data = json.dumps({"requests": [{"type": "execute", "stmt": stmt},
                                   {"type": "close"}]}).encode("utf-8")
    request = Request(URL, data=data, method="POST", headers={
        "Authorization": "Bearer " + load_token(), "Content-Type": "application/json"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            body = response.read(2 * 1024 * 1024 + 1)
    except HTTPError as exc:
        raise RuntimeError("Database HTTP %s; check token, expiry and free quota." % exc.code) from None
    if len(body) > 2 * 1024 * 1024:
        raise RuntimeError("Response too large; request fewer rows.")
    result = json.loads(body)["results"][0]
    if result["type"] != "ok":
        raise RuntimeError("Database rejected the operation; verify schema and permission.")
    value = result["response"]["result"]
    names = [col["name"] for col in value.get("cols", [])]
    def decode(cell):
        if cell["type"] == "null":
            return None
        if cell["type"] == "integer":
            return int(cell["value"])
        return cell.get("value") if cell["type"] != "blob" else cell.get("base64")
    return [dict(zip(names, map(decode, row))) for row in value.get("rows", [])]


def key(args, name):
    value = args.get(name)
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 for c in value):
        raise ValueError(name + " must be a nonempty identifier of at most 128 characters.")
    return value


def call_tool(name, args):
    if name == "list_datasets":
        return execute("SELECT d.*, (SELECT COUNT(*) FROM files f WHERE f.dataset_id=d.dataset_id) AS file_count FROM datasets d ORDER BY d.dataset_id LIMIT 100")
    if name == "list_files":
        dataset = key(args, "dataset_id")
        limit = args.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be 1-100.")
        return execute("SELECT * FROM files WHERE dataset_id=? ORDER BY remote_path LIMIT ?", (dataset, limit))
    if name == "write_result":
        project, result = key(args, "project_id"), key(args, "result_key")
        if "value" not in args:
            raise ValueError("value is required.")
        payload = json.dumps(args["value"], ensure_ascii=False, allow_nan=False)
        if len(payload.encode("utf-8")) > 65536:
            raise ValueError("One structured result is limited to 64 KB; store large files in the cloud file archive.")
        execute("INSERT INTO project_results(project_id,result_key,dataset_id,result_json) VALUES (?,?,?,?) ON CONFLICT(project_id,result_key) DO UPDATE SET dataset_id=excluded.dataset_id,result_json=excluded.result_json,updated_at=CURRENT_TIMESTAMP",
                (project, result, args.get("dataset_id"), payload))
        return {"saved": True, "project_id": project, "result_key": result}
    if name == "read_result":
        rows = execute("SELECT result_json,updated_at FROM project_results WHERE project_id=? AND result_key=?",
                       (key(args, "project_id"), key(args, "result_key")))
        return {"found": False} if not rows else {"found": True, "value": json.loads(rows[0]["result_json"]), "updated_at": rows[0]["updated_at"]}
    if name == "list_results":
        return execute("SELECT project_id,result_key,dataset_id,updated_at FROM project_results WHERE project_id=? ORDER BY result_key LIMIT 100",
                       (key(args, "project_id"),))
    raise ValueError("Unknown tool.")


def tool(name, description, properties=None, required=None, write=False):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties or {}, "required": required or [],
        "additionalProperties": False}, "annotations": {"readOnlyHint": not write,
        "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}}


TEXT = {"type": "string", "minLength": 1, "maxLength": 128}
TOOLS = [
    tool("list_datasets", "List shared cloud datasets and catalogued file counts. Catalogued does not mean transferred."),
    tool("list_files", "Read a dataset's cloud file catalog, verification status, hashes and archive links.",
         {"dataset_id": TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["dataset_id"]),
    tool("write_result", "Save structured JSON in this project's namespace. Reusing a result key updates that project's value. No raw files.",
         {"project_id": TEXT, "result_key": TEXT, "dataset_id": TEXT, "value": {}}, ["project_id", "result_key", "value"], True),
    tool("read_result", "Read one structured result from an explicitly named project.",
         {"project_id": TEXT, "result_key": TEXT}, ["project_id", "result_key"]),
    tool("list_results", "List result keys for one project, without loading all result bodies.",
         {"project_id": TEXT}, ["project_id"]),
]


def handle(message):
    method = message.get("method")
    if "id" not in message:
        return None
    response = {"jsonrpc": "2.0", "id": message["id"]}
    if method == "initialize":
        wanted = message.get("params", {}).get("protocolVersion")
        response["result"] = {"protocolVersion": wanted if wanted in (
            "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "research_data", "version": "0.1.0"},
            "instructions": "Shared Turso research catalog. All data is remote. Use a distinct project_id for each project. Store only small structured results here; archive raw files separately. A catalogued file has not necessarily been transferred. Never request, return or log credentials. This server exposes no arbitrary SQL or delete tools."}
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params", {})
        try:
            result = call_tool(params["name"], params.get("arguments", {}))
            response["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            response["result"] = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    else:
        response["error"] = {"code": -32601, "message": "Method not supported"}
    return response


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    while True:
        line = sys.stdin.buffer.readline(1024 * 1024 + 1)
        if not line:
            break
        if len(line) > 1024 * 1024:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request too large"}}), flush=True)
            break
        try:
            reply = handle(json.loads(line))
        except Exception:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid request"}}
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
