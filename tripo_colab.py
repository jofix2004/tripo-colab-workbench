import base64
import hashlib
import hmac
import json
import mimetypes
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import gradio as gr
import requests


TRIPO_BASE = "https://api.tripo3d.ai/v2/openapi"
FINAL_STATUSES = {"success", "failed", "banned", "expired", "cancelled", "unknown"}
DATA_DIR = Path(os.environ.get("TRIPO_COLAB_HOME", "/content/tripo_colab"))
STORE_FILE = DATA_DIR / "tripo-history.json"
CACHE_DIR = DATA_DIR / "cache"
MODEL_CACHE_DIR = CACHE_DIR / "models"
PREVIEW_CACHE_DIR = CACHE_DIR / "previews"


def ensure_dirs():
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not STORE_FILE.exists():
        write_store({"version": 1, "updated_at": now_iso(), "tasks": [], "cache": []})


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def read_store():
    ensure_dirs()
    try:
        return json.loads(STORE_FILE.read_text("utf-8"))
    except Exception:
        return {"version": 1, "updated_at": now_iso(), "tasks": [], "cache": []}


def write_store(store):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated_at": now_iso(),
        "tasks": list(store.get("tasks") or [])[:200],
        "cache": list(store.get("cache") or []),
    }
    STORE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False), "utf-8")


def upsert_task(record):
    if not record or not record.get("task_id"):
        return record
    store = read_store()
    task_id = record["task_id"]
    tasks = store.get("tasks") or []
    merged = False
    for idx, item in enumerate(tasks):
        if item.get("task_id") == task_id:
            tasks[idx] = {**item, **record, "updated_at": now_iso()}
            merged = True
            break
    if not merged:
        tasks.insert(0, {**record, "created_at": record.get("created_at") or now_iso(), "updated_at": now_iso()})
    store["tasks"] = tasks[:200]
    write_store(store)
    return record


def auth_headers(api_key, content_type=None):
    headers = {"Authorization": f"Bearer {api_key.strip()}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def require_key(api_key):
    api_key = (api_key or "").strip()
    if not api_key:
        raise gr.Error("Nhap API key")
    return api_key


def post_task(api_key, payload):
    api_key = require_key(api_key)
    response = requests.post(
        f"{TRIPO_BASE}/task",
        headers=auth_headers(api_key, "application/json"),
        json=prune(payload),
        timeout=120,
    )
    data = parse_response(response)
    task_id = data.get("data", {}).get("task_id") or data.get("task_id")
    if not task_id:
        raise gr.Error(f"No task_id: {data}")
    record = {
        "task_id": task_id,
        "type": payload.get("type", ""),
        "status": "queued",
        "progress": 0,
        "request": prune(payload),
        "last_response": data,
    }
    upsert_task(record)
    return task_id


def get_task(api_key, task_id):
    api_key = require_key(api_key)
    task_id = (task_id or "").strip()
    if not task_id:
        raise gr.Error("Missing task id")
    response = requests.get(f"{TRIPO_BASE}/task/{quote(task_id)}", headers=auth_headers(api_key), timeout=120)
    data = parse_response(response)
    task = data.get("data") or data
    record = normalize_task(task)
    if record.get("task_id"):
        upsert_task(record)
    return task


def parse_response(response):
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text}
    if response.status_code >= 400 or (isinstance(data, dict) and data.get("code") not in (None, 0)):
        message = data.get("message") or data.get("error") or response.text
        raise gr.Error(str(message))
    return data


def normalize_task(task):
    task_id = task.get("task_id") or task.get("id") or ""
    asset = extract_assets(task)
    return {
        "task_id": task_id,
        "type": task.get("type", ""),
        "status": task.get("status", "queued"),
        "progress": task.get("progress", 0),
        "queuing_num": task.get("queuing_num", -1),
        "running_left_time": task.get("running_left_time", -1),
        "consumed_credit": task.get("consumed_credit", 0),
        "input": task.get("input"),
        "output": task.get("output"),
        "result": task.get("result"),
        "thumbnail": task.get("thumbnail"),
        "model_url": asset.get("model_url", ""),
        "preview_url": asset.get("preview_url", ""),
        "last_response": task,
    }


def extract_assets(task):
    output = task.get("output") or {}
    result = task.get("result") or output.get("result") or {}
    base = result.get("base_model") or output.get("base_model") or {}
    rendered = result.get("rendered_image") or output.get("rendered_image") or {}
    thumb = task.get("thumbnail") or result.get("thumbnail") or output.get("thumbnail") or ""
    model_url = base.get("url") if isinstance(base, dict) else base
    preview_url = rendered.get("url") if isinstance(rendered, dict) else rendered
    if not preview_url:
        preview_url = thumb.get("url") if isinstance(thumb, dict) else thumb
    return {"model_url": model_url or "", "preview_url": preview_url or ""}


def poll_until_done(api_key, task_id, max_wait=900):
    start = time.time()
    last = {}
    while time.time() - start <= max_wait:
        last = get_task(api_key, task_id)
        status = str(last.get("status", "")).lower()
        if status in FINAL_STATUSES:
            break
        time.sleep(2)
    record = normalize_task(last)
    model_path, preview_path = cache_task_assets(record)
    record["cached_model_path"] = str(model_path) if model_path else ""
    record["cached_preview_path"] = str(preview_path) if preview_path else ""
    upsert_task(record)
    return record, model_path, preview_path


def upload_image(api_key, path):
    api_key = require_key(api_key)
    path = normalize_upload_path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    with path.open("rb") as handle:
        files = {"file": (path.name, handle, mime)}
        response = requests.post(f"{TRIPO_BASE}/upload/sts", headers=auth_headers(api_key), files=files, timeout=120)
    data = parse_response(response)
    payload = data.get("data") or data
    token = payload.get("image_token") or payload.get("file_token")
    if not token:
        raise gr.Error(f"No file token: {data}")
    return token


def upload_model(api_key, path):
    api_key = require_key(api_key)
    path = normalize_upload_path(path)
    fmt = path.suffix.lower().lstrip(".")
    if fmt == "gltf":
        fmt = "glb"
    if fmt not in {"glb", "obj", "fbx", "stl"}:
        raise gr.Error("Model file must be .glb, .obj, .fbx, or .stl")

    token_res = requests.post(
        f"{TRIPO_BASE}/upload/sts/token",
        headers=auth_headers(api_key, "application/json"),
        json={"format": fmt},
        timeout=120,
    )
    token_json = parse_response(token_res)
    data = token_json.get("data") or {}
    body = path.read_bytes()
    host = data.get("s3_host") or "s3.us-west-2.amazonaws.com"
    bucket = data.get("resource_bucket") or "tripo-data"
    key = data.get("resource_uri")
    session_token = data.get("session_token")
    access_key = data.get("sts_ak")
    secret_key = data.get("sts_sk")
    if not all([host, bucket, key, session_token, access_key, secret_key]):
        raise gr.Error(f"STS token response missing fields: {token_json}")

    upload_url = f"https://{host}/{bucket}/{encode_s3_key(key)}"
    headers = sign_s3_put(host, bucket, key, body, session_token, access_key, secret_key)
    headers["Content-Type"] = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    upload_res = requests.put(upload_url, headers=headers, data=body, timeout=300)
    if upload_res.status_code >= 400:
        raise gr.Error(upload_res.text or f"S3 upload failed ({upload_res.status_code})")
    return {"bucket": bucket, "key": key}


def sign_s3_put(host, bucket, key, body, session_token, access_key, secret_key):
    payload_hash = hashlib.sha256(body).hexdigest()
    now = datetime.utcnow()
    date_stamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    signed_headers = "host;x-amz-content-sha256;x-amz-date;x-amz-security-token"
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
        f"x-amz-security-token:{session_token}\n"
    )
    canonical_request = "\n".join([
        "PUT",
        f"/{bucket}/{encode_s3_key(key)}",
        "",
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{date_stamp}/us-west-2/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signature = hmac_sha256(signing_key(secret_key, date_stamp, "us-west-2", "s3"), string_to_sign, hex_output=True)
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "x-amz-security-token": session_token,
    }


def hmac_sha256(key, data, hex_output=False):
    digest = hmac.new(key if isinstance(key, bytes) else key.encode(), data.encode(), hashlib.sha256).digest()
    return digest.hex() if hex_output else digest


def signing_key(secret_key, date_stamp, region, service):
    k_date = hmac_sha256(f"AWS4{secret_key}", date_stamp)
    k_region = hmac_sha256(k_date, region)
    k_service = hmac_sha256(k_region, service)
    return hmac_sha256(k_service, "aws4_request")


def encode_s3_key(key):
    return "/".join(quote(part, safe="") for part in key.split("/"))


def normalize_upload_path(value):
    if value is None:
        raise gr.Error("Missing file")
    if isinstance(value, str):
        return Path(value)
    if hasattr(value, "name"):
        return Path(value.name)
    raise gr.Error("Unsupported file input")


def file_type(path):
    suffix = normalize_upload_path(path).suffix.lower().lstrip(".")
    return "jpg" if suffix == "jpeg" else suffix or "png"


def cache_task_assets(record):
    model_path = cache_url(record.get("model_url"), "model", record.get("task_id"))
    preview_path = cache_url(record.get("preview_url"), "preview", record.get("task_id"))
    return model_path, preview_path


def cache_url(url, kind, task_id=""):
    if not url or not str(url).startswith("http"):
        return None
    out_dir = MODEL_CACHE_DIR if kind == "model" else PREVIEW_CACHE_DIR
    ext = Path(str(url).split("?")[0]).suffix or (".webp" if kind == "preview" else ".glb")
    name = hashlib.sha256(str(url).split("?")[0].encode()).hexdigest()[:24] + ext
    target = out_dir / name
    if target.exists() and target.stat().st_size > 0:
        return target
    response = requests.get(url, timeout=300)
    if response.status_code >= 400:
        return None
    target.write_bytes(response.content)
    store = read_store()
    store.setdefault("cache", []).insert(0, {
        "kind": kind,
        "task_id": task_id,
        "source_url": url,
        "local_path": str(target),
        "bytes": target.stat().st_size,
        "cached_at": now_iso(),
    })
    write_store(store)
    return target


def prune(value):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            clean = prune(item)
            if clean is not None and clean != "":
                out[key] = clean
        return out
    if isinstance(value, list):
        return [prune(item) for item in value if prune(item) is not None]
    return value


def optional_int(value):
    if value in (None, ""):
        return None
    return int(value)


def generation_payload(task_type, version, face_limit, smart, quad, geometry_quality, model_seed):
    payload = {
        "type": task_type,
        "model_version": version,
        "texture": False,
        "pbr": False,
        "export_uv": False,
    }
    if face_limit not in (None, ""):
        payload["face_limit"] = int(face_limit)
    if smart and version != "P1-20260311":
        payload["smart_low_poly"] = True
    if quad and version != "P1-20260311":
        payload["quad"] = True
    if geometry_quality == "detailed" and version in {"v3.1-20260211", "v3.0-20250812"}:
        payload["geometry_quality"] = "detailed"
    if model_seed not in (None, ""):
        payload["model_seed"] = int(model_seed)
    return payload


def run_image_to_model(api_key, image, version, face_limit, autofix, smart, quad, geometry_quality, seed):
    token = upload_image(api_key, image)
    payload = generation_payload("image_to_model", version, face_limit, smart, quad, geometry_quality, seed)
    payload["file"] = {"type": file_type(image), "file_token": token}
    if autofix:
        payload["enable_image_autofix"] = True
    return run_and_render(api_key, payload)


def run_multiview_to_model(api_key, original_task_id, front, left, back, right, version, face_limit, autofix, smart, quad, geometry_quality, seed):
    payload = generation_payload("multiview_to_model", version, face_limit, smart, quad, geometry_quality, seed)
    if original_task_id:
        payload["original_task_id"] = original_task_id.strip()
    else:
        files = []
        paths = [front, left, back, right]
        if not front:
            raise gr.Error("Need front image")
        if sum(1 for item in paths if item) < 2:
            raise gr.Error("Need at least two view images")
        for path in paths:
            if not path:
                files.append({"type": "jpg"})
                continue
            files.append({"type": file_type(path), "file_token": upload_image(api_key, path)})
        payload["files"] = files
    if autofix:
        payload["enable_image_autofix"] = True
    return run_and_render(api_key, payload)


def run_import_model(api_key, model_file):
    obj = upload_model(api_key, model_file)
    payload = {"type": "import_model", "file": {"object": obj}}
    return run_and_render(api_key, payload)


def run_lowpoly(api_key, original_task_id, version, face_limit, quad, bake, part_names):
    payload = {
        "type": "highpoly_to_lowpoly",
        "original_model_task_id": (original_task_id or "").strip(),
        "model_version": version,
    }
    if not payload["original_model_task_id"]:
        raise gr.Error("Need original model task id")
    if face_limit not in (None, ""):
        payload["face_limit"] = int(face_limit)
    if quad:
        payload["quad"] = True
    if not bake:
        payload["bake"] = False
    names = split_csv(part_names)
    if names:
        payload["part_names"] = names
    return run_and_render(api_key, payload)


def run_convert(api_key, original_task_id, fmt, face_limit, quad, flatten_bottom, flatten_threshold, pivot, scale_factor, fbx_preset, export_orientation):
    payload = {
        "type": "convert_model",
        "original_model_task_id": (original_task_id or "").strip(),
        "format": fmt,
    }
    if not payload["original_model_task_id"]:
        raise gr.Error("Need original model task id")
    if face_limit not in (None, ""):
        payload["face_limit"] = int(face_limit)
    if quad:
        payload["quad"] = True
    if flatten_bottom:
        payload["flatten_bottom"] = True
    if flatten_threshold not in (None, "", 0.01):
        payload["flatten_bottom_threshold"] = float(flatten_threshold)
    if pivot:
        payload["pivot_to_center_bottom"] = True
    if scale_factor not in (None, "", 1):
        payload["scale_factor"] = float(scale_factor)
    if fbx_preset != "blender":
        payload["fbx_preset"] = fbx_preset
    if export_orientation != "+x":
        payload["export_orientation"] = export_orientation
    return run_and_render(api_key, payload)


def run_and_render(api_key, payload):
    task_id = post_task(api_key, payload)
    record, model_path, preview_path = poll_until_done(api_key, task_id)
    status = render_status(record)
    model = str(model_path) if model_path and model_path.suffix.lower() in {".glb", ".gltf", ".obj", ".stl"} else None
    preview = str(preview_path) if preview_path else None
    raw = json.dumps(record.get("last_response") or record, indent=2, ensure_ascii=False)
    return status, model, preview, raw, render_history()


def refresh_task(api_key, task_id):
    record, model_path, preview_path = poll_until_done(api_key, task_id, max_wait=5)
    return render_status(record), str(model_path) if model_path else None, str(preview_path) if preview_path else None, json.dumps(record, indent=2, ensure_ascii=False), render_history()


def render_status(record):
    return (
        f"task_id: {record.get('task_id', '-')}\n"
        f"type: {record.get('type', '-')}\n"
        f"status: {record.get('status', '-')}\n"
        f"progress: {record.get('progress', 0)}\n"
        f"credits: {record.get('consumed_credit', 0)}\n"
        f"model: {record.get('model_url', '')}\n"
        f"preview: {record.get('preview_url', '')}"
    )


def render_history():
    store = read_store()
    rows = []
    for task in store.get("tasks", [])[:100]:
        rows.append([
            task.get("task_id", ""),
            task.get("type", ""),
            task.get("status", ""),
            task.get("progress", 0),
            task.get("consumed_credit", 0),
            task.get("model_url", ""),
            task.get("cached_model_path", ""),
        ])
    return rows


def split_csv(value):
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def build_app():
    ensure_dirs()
    css = """
    .preview-col { order: 2; }
    .controls-col { order: 1; }
    #preview-model { min-height: 720px !important; }
    #preview-image { min-height: 220px !important; }
    #raw-json pre, #raw-json textarea { max-height: 180px !important; font-size: 11px !important; }
    #history-box table { font-size: 11px !important; }
    """
    with gr.Blocks(title="Tripo API Workbench Colab", css=css) as app:
        gr.Markdown("## Tripo API Workbench - Colab\nTexture/PBR off by default. History/cache in `/content/tripo_colab` unless `TRIPO_COLAB_HOME` is set.")
        with gr.Row():
            with gr.Column(scale=3, min_width=620, elem_id="preview-col", elem_classes=["preview-col"]):
                model = gr.Model3D(label="3D preview", elem_id="preview-model")
                preview = gr.Image(label="Preview image", type="filepath", elem_id="preview-image")
                with gr.Accordion("Raw task JSON", open=False):
                    raw = gr.Code(label="Raw task JSON", language="json", elem_id="raw-json")
                with gr.Accordion("History", open=False):
                    history_table = gr.Dataframe(
                        headers=["task_id", "type", "status", "progress", "credits", "model_url", "cached_model_path"],
                        value=render_history(),
                        label="History",
                        elem_id="history-box",
                    )
            with gr.Column(scale=2, min_width=520, elem_id="controls-col", elem_classes=["controls-col"]):
                api_key = gr.Textbox(label="API key", type="password", placeholder="tsk_...")
                status = gr.Textbox(label="Task status", lines=8)
                with gr.Tabs():
                    with gr.Tab("Image to model"):
                        image = gr.File(label="Image", file_types=["image"])
                        with gr.Row():
                            version = gr.Dropdown(["v3.1-20260211", "P1-20260311", "Turbo-v1.0-20250506", "v3.0-20250812", "v2.5-20250123", "v2.0-20240919"], value="v3.1-20260211", label="Model version")
                            face = gr.Number(label="Face limit", precision=0)
                        with gr.Row():
                            autofix = gr.Checkbox(label="Image autofix")
                            smart = gr.Checkbox(label="Smart mesh")
                            quad = gr.Checkbox(label="Quad mesh")
                        with gr.Row():
                            geo = gr.Dropdown(["standard", "detailed"], value="standard", label="Geometry quality")
                            seed = gr.Number(label="Model seed", precision=0)
                        run = gr.Button("Run image_to_model", variant="primary")
                        run.click(run_image_to_model, [api_key, image, version, face, autofix, smart, quad, geo, seed], [status, model, preview, raw, history_table])

                    with gr.Tab("Multiview to model"):
                        original = gr.Textbox(label="Original multiview task id")
                        with gr.Row():
                            front = gr.File(label="Front", file_types=["image"])
                            left = gr.File(label="Left", file_types=["image"])
                            back = gr.File(label="Back", file_types=["image"])
                            right = gr.File(label="Right", file_types=["image"])
                        with gr.Row():
                            mv_version = gr.Dropdown(["v3.1-20260211", "P1-20260311", "v3.0-20250812", "v2.5-20250123", "v2.0-20240919"], value="v3.1-20260211", label="Model version")
                            mv_face = gr.Number(label="Face limit", precision=0)
                        with gr.Row():
                            mv_autofix = gr.Checkbox(label="Image autofix")
                            mv_smart = gr.Checkbox(label="Smart mesh")
                            mv_quad = gr.Checkbox(label="Quad mesh")
                        with gr.Row():
                            mv_geo = gr.Dropdown(["standard", "detailed"], value="standard", label="Geometry quality")
                            mv_seed = gr.Number(label="Model seed", precision=0)
                        run = gr.Button("Run multiview_to_model", variant="primary")
                        run.click(run_multiview_to_model, [api_key, original, front, left, back, right, mv_version, mv_face, mv_autofix, mv_smart, mv_quad, mv_geo, mv_seed], [status, model, preview, raw, history_table])

                    with gr.Tab("Import model"):
                        model_file = gr.File(label="Model", file_types=[".glb", ".obj", ".fbx", ".stl"])
                        run = gr.Button("Run import_model", variant="primary")
                        run.click(run_import_model, [api_key, model_file], [status, model, preview, raw, history_table])

                    with gr.Tab("Smart low poly"):
                        low_id = gr.Textbox(label="Original model task id")
                        with gr.Row():
                            low_version = gr.Dropdown(["P-v2.0-20251226", "P-v2.0-20251225", "v1.0-20250506"], value="P-v2.0-20251226", label="Model version")
                            low_face = gr.Slider(500, 20000, value=20000, step=1, label="Face limit")
                        with gr.Row():
                            low_quad = gr.Checkbox(label="Quad mesh")
                            low_bake = gr.Checkbox(label="Bake", value=True)
                        low_parts = gr.Textbox(label="Part names")
                        run = gr.Button("Run smart low poly", variant="primary")
                        run.click(run_lowpoly, [api_key, low_id, low_version, low_face, low_quad, low_bake, low_parts], [status, model, preview, raw, history_table])

                    with gr.Tab("Conversion"):
                        convert_id = gr.Textbox(label="Original model task id")
                        with gr.Row():
                            fmt = gr.Dropdown(["GLTF", "USDZ", "FBX", "OBJ", "STL", "3MF"], value="GLTF", label="Format")
                            conv_face = gr.Slider(500, 20000, value=20000, step=1, label="Face limit")
                        with gr.Row():
                            conv_quad = gr.Checkbox(label="Quad mesh")
                            flatten = gr.Checkbox(label="Flatten bottom")
                            pivot = gr.Checkbox(label="Pivot center bottom")
                        with gr.Row():
                            threshold = gr.Number(value=0.01, label="Flatten threshold")
                            scale = gr.Number(value=1, label="Scale factor")
                        with gr.Row():
                            preset = gr.Dropdown(["blender", "mixamo", "3dsmax"], value="blender", label="FBX preset")
                            orient = gr.Dropdown(["+x", "+y", "-x", "-y"], value="+x", label="Export orientation")
                        run = gr.Button("Run convert_model", variant="primary")
                        run.click(run_convert, [api_key, convert_id, fmt, conv_face, conv_quad, flatten, threshold, pivot, scale, preset, orient], [status, model, preview, raw, history_table])

                    with gr.Tab("History"):
                        refresh_id = gr.Textbox(label="Task id")
                        refresh = gr.Button("Refresh task")
                        refresh.click(refresh_task, [api_key, refresh_id], [status, model, preview, raw, history_table])
                        reload_history = gr.Button("Reload history")
                        reload_history.click(render_history, None, history_table)

    return app


if __name__ == "__main__":
    build_app().launch(share=True, debug=True)
