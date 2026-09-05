import os
import yaml

def load_config(config_path="config.yaml") -> dict:
    """Read config.yaml file if it exists, returning a dictionary."""
    if not os.path.exists(config_path):
        return {"cameras": []}
    
    with open(config_path, "r") as f:
        try:
            config = yaml.safe_load(f)
            if not config or "cameras" not in config:
                return {"cameras": []}
            return config
        except Exception:
            return {"cameras": []}

def save_config(config: dict, config_path="config.yaml"):
    """Write config dictionary to config.yaml."""
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)

def append_camera_config(
    camera_id: str,
    ip_address: str = "",
    rtsp_url: str = "",
    status: str = "active",
    source: dict | None = None,
    name: str = "",
    config_path: str = "config.yaml",
):
    """
    Appends or updates a camera configuration inside config.yaml.
    Guarantees no duplicate ID collisions.
    Supports both legacy flat rtsp_url and modern structured source definitions.
    """
    config = load_config(config_path)
    if "cameras" not in config or config["cameras"] is None:
        config["cameras"] = []

    # Check for existing camera with same ID to update it
    existing_idx = None
    existing_cam = {}
    for idx, cam in enumerate(config["cameras"]):
        if cam.get("id") == camera_id or cam.get("camera_id") == camera_id:
            existing_idx = idx
            existing_cam = cam
            break

    cam_entry = dict(existing_cam)
    cam_entry.update({
        "id": camera_id,
        "ip": ip_address or cam_entry.get("ip", ""),
        "rtsp_url": rtsp_url if rtsp_url else cam_entry.get("rtsp_url", ""),
        "status": status,
    })
    if name:
        cam_entry["name"] = name

    if source:
        cam_entry["source"] = source

    if existing_idx is not None:
        config["cameras"][existing_idx] = cam_entry
    else:
        config["cameras"].append(cam_entry)

    save_config(config, config_path)


def update_camera_config(
    camera_id: str,
    updates: dict,
    config_path: str = "config.yaml",
) -> dict | None:
    """
    Updates an existing camera's configuration in config_path.
    Preserves all existing fields not present in updates.
    Returns the updated camera dict if found, or None.
    """
    config = load_config(config_path)
    if "cameras" not in config or not isinstance(config["cameras"], list):
        config["cameras"] = []

    clean_id = camera_id.strip()
    target_idx = None
    target_cam = None

    for idx, cam in enumerate(config["cameras"]):
        cid = str(cam.get("id") or cam.get("camera_id") or "").strip()
        if cid == clean_id or cid.lower() == clean_id.lower():
            target_idx = idx
            target_cam = dict(cam)
            break

    # If camera is CAM_01 (or local node alias) and not explicitly declared, auto-initialize
    if target_idx is None or target_cam is None:
        if clean_id.upper() in ("CAM_01", "CAM01", "CAM-01"):
            target_cam = {
                "id": "CAM_01",
                "name": "LOCAL_LAPTOP_NODE",
                "rtsp_url": "http://host.docker.internal:8085/video_feed",
                "status": "active",
                "location": {
                    "lat": 28.6139,
                    "lng": 77.2090,
                    "address": "Local Laptop Node",
                },
                "source": {
                    "type": "direct",
                    "url": "http://host.docker.internal:8085/video_feed",
                },
            }
            target_idx = len(config["cameras"])
            config["cameras"].append(target_cam)
        else:
            return None

    # Merge permitted update keys
    for k, v in updates.items():
        if v is not None:
            if k == "location" and isinstance(v, dict) and isinstance(target_cam.get("location"), dict):
                merged_loc = dict(target_cam["location"])
                for lk, lv in v.items():
                    if lv is not None and lv != "":
                        merged_loc[lk] = lv
                target_cam["location"] = merged_loc
            else:
                target_cam[k] = v

    # Support flat address, lat, lng if provided
    if "address" in updates and updates["address"] is not None:
        if not isinstance(target_cam.get("location"), dict):
            target_cam["location"] = {}
        target_cam["location"]["address"] = str(updates["address"]).strip()
    if "lat" in updates and updates["lat"] not in (None, ""):
        if not isinstance(target_cam.get("location"), dict):
            target_cam["location"] = {}
        try:
            target_cam["location"]["lat"] = float(updates["lat"])
        except (ValueError, TypeError):
            pass
    if "lng" in updates and updates["lng"] not in (None, ""):
        if not isinstance(target_cam.get("location"), dict):
            target_cam["location"] = {}
        try:
            target_cam["location"]["lng"] = float(updates["lng"])
        except (ValueError, TypeError):
            pass

    # Synchronize rtsp_url with source definition if direct stream
    if "rtsp_url" in updates and updates["rtsp_url"]:
        new_url = updates["rtsp_url"]
        target_cam["rtsp_url"] = new_url
        if isinstance(target_cam.get("source"), dict):
            target_cam["source"]["url"] = new_url

    config["cameras"][target_idx] = target_cam
    save_config(config, config_path)
    return target_cam


def set_camera_status(
    camera_id: str,
    status: str,
    config_path: str = "config.yaml",
) -> bool:
    """
    Sets camera status to 'active', 'disabled', or 'inactive' in config_path.
    Returns True if camera was found and status updated, False otherwise.
    """
    res = update_camera_config(camera_id, {"status": status.lower()}, config_path=config_path)
    return res is not None


def delete_camera_config(
    camera_id: str,
    config_path: str = "config.yaml",
) -> bool:
    """
    Deletes a camera configuration from config_path.
    Does NOT delete historical alerts, snapshots, clips, or database records.
    Returns True if camera was found and removed, False otherwise.
    """
    config = load_config(config_path)
    if "cameras" not in config or not config["cameras"]:
        return False

    clean_id = camera_id.strip().lower()
    initial_len = len(config["cameras"])
    config["cameras"] = [
        cam for cam in config["cameras"]
        if str(cam.get("id") or cam.get("camera_id") or "").strip().lower() != clean_id
    ]

    if len(config["cameras"]) < initial_len:
        save_config(config, config_path)
        return True
    return False



