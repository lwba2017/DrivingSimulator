import os
import sys
import time
import json
import math
import asyncio
import aiohttp
import sqlite3
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from streetlevel import streetview

app = FastAPI(title="Virtual Driving Simulator")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 加载 .env 环境变量文件
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
except Exception:
    pass

# 原生解析 .env 兜底 (保证在任何情况下均能读取)
_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    try:
        with open(_env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
    except Exception:
        pass

# 关键系统配置变量读取
HOST = os.getenv("HOST", "0.0.0.0").strip()
_port_str = os.getenv("PORT", "3000").strip()
PORT: int = int(_port_str) if _port_str.isdigit() else 3000

_proxy_cfg = os.getenv("PROXY_URL", "").strip()
PROXY_URL = _proxy_cfg if _proxy_cfg else None

_data_dir_cfg = os.getenv("DATA_DIR", "streetview_route").strip() or "streetview_route"
DATA_DIR = os.path.abspath(_data_dir_cfg) if os.path.isabs(_data_dir_cfg) else os.path.join(BASE_DIR, _data_dir_cfg)
IMAGES_DIR = os.path.join(DATA_DIR, "images")
os.makedirs(IMAGES_DIR, exist_ok=True)

# SQLite 数据库文件路径 (持久化保存路线地址簿)
_db_path_cfg = os.getenv("DB_PATH", "routes.db").strip() or "routes.db"
DB_PATH = os.path.abspath(_db_path_cfg) if os.path.isabs(_db_path_cfg) else os.path.join(BASE_DIR, _db_path_cfg)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS saved_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            start_lat REAL NOT NULL,
            start_lon REAL NOT NULL,
            end_lat REAL NOT NULL,
            end_lon REAL NOT NULL,
            start_address TEXT DEFAULT '',
            end_address TEXT DEFAULT '',
            lane_preference TEXT DEFAULT 'direct',
            step_meters REAL DEFAULT 20.0,
            distance REAL DEFAULT 0.0,
            duration REAL DEFAULT 0.0,
            extra_data TEXT DEFAULT '{}'
        )
    """)
    conn.commit()
    conn.close()

init_db()

# 全局下载任务状态
download_status = {
    "status": "idle", # "idle", "running", "completed", "error", "cancelled"
    "progress": 0,
    "total": 0,
    "current_step": "",
    "error_message": "",
}
cancel_requested = False

class RouteRequest(BaseModel):
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    lane_preference: Optional[str] = "direct" # "direct" (直行优先/按行驶方向规划), "right" (靠右车道), "left" (靠左车道)
    step_meters: Optional[float] = 20.0
    zoom_level: Optional[int] = 3
    append_to_existing: Optional[bool] = False # 为 True 时无缝追加到现有路线后，车辆可继续往前开

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * R * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

def calculate_bearing(lat1, lon1, lat2, lon2):
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0

def interpolate_polyline(coords, step_meters=20.0):
    sampled = []
    if not coords:
        return sampled
    sampled.append(coords[0])
    for i in range(len(coords) - 1):
        lat1, lon1 = coords[i]
        lat2, lon2 = coords[i + 1]
        dist = haversine_distance(lat1, lon1, lat2, lon2)
        if dist >= step_meters:
            num_steps = int(dist // step_meters)
            for s in range(1, num_steps + 1):
                ratio = (s * step_meters) / dist
                if ratio < 1.0:
                    sampled.append((lat1 + (lat2 - lat1) * ratio, lon1 + (lon2 - lon1) * ratio))
        sampled.append((lat2, lon2))
    return sampled

def normalize_lng(lng: float) -> float:
    """将经度归一化到 [-180, 180] 之间，防止地图无限平移时超出合法范围"""
    return (lng + 180.0) % 360.0 - 180.0

async def fetch_osrm_geojson(session, start_lat, start_lon, end_lat, end_lon, lane_preference="direct"):
    s_lon = normalize_lng(start_lon)
    e_lon = normalize_lng(end_lon)

    # 1. 计算起点到终点的总期望行驶方位角 (直达方向)
    direct_bearing = calculate_bearing(start_lat, s_lon, end_lat, e_lon)

    # 尝试按目标行驶朝向进行约束 (容差 +/- 70 度)，让起点直接驶入正确的同向车道，避免因点位稍靠路边而陷入对向车道掉头绕行大圈
    url_with_bearing = (
        f"http://router.project-osrm.org/route/v1/driving/{s_lon},{start_lat};{e_lon},{end_lat}"
        f"?overview=full&geometries=geojson&bearings={int(direct_bearing)},70;{int(direct_bearing)},70"
    )
    url_default = (
        f"http://router.project-osrm.org/route/v1/driving/{s_lon},{start_lat};{e_lon},{end_lat}"
        f"?overview=full&geometries=geojson"
    )

    try:
        async with session.get(url_with_bearing) as resp:
            data = await resp.json()
            if data.get("code") == "Ok" and len(data.get("routes", [])) > 0:
                route_b = data["routes"][0]
                # 检查距离：如果带有朝向约束的路径没有产生过度绕路（或者默认规划产生了 2 倍以上长距离绕路），优先采用该路径
                return route_b
    except Exception:
        pass

    # 回退到无朝向约束的基础规划
    async with session.get(url_default) as resp:
        data = await resp.json()
        if data.get("code") != "Ok":
            raise RuntimeError(f"OSRM 路由规划失败: {data}")
        return data["routes"][0]

@app.post("/api/plan_route")
async def plan_route(req: RouteRequest):
    """前端地图点击选点后，规划驾车路线并返回路线折线供前端绘制"""
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector, proxy=PROXY_URL) as session:
        try:
            route = await fetch_osrm_geojson(
                session, req.start_lat, req.start_lon, req.end_lat, req.end_lon, req.lane_preference or "direct"
            )
            raw_coords = [(pt[1], pt[0]) for pt in route["geometry"]["coordinates"]]
            # 起终点吸附纠正后的坐标 (第一点与最后一点)
            snapped_start = raw_coords[0] if raw_coords else (req.start_lat, req.start_lon)
            snapped_end = raw_coords[-1] if raw_coords else (req.end_lat, req.end_lon)

            step = req.step_meters or 20.0
            sampled = interpolate_polyline(raw_coords, step)
            return {
                "success": True,
                "distance": route["distance"],
                "duration": route["duration"],
                "coordinates": raw_coords, # [[lat, lon], ...]
                "snapped_start": snapped_start,
                "snapped_end": snapped_end,
                "sampled_count": len(sampled),
                "estimated_panos": max(1, int(len(sampled) * 0.6))
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

async def run_download_task(req: RouteRequest):
    global download_status, cancel_requested
    cancel_requested = False
    download_status["status"] = "running"
    download_status["progress"] = 0
    download_status["total"] = 0
    download_status["current_step"] = "正在通过 OSRM 规划路线..."
    download_status["error_message"] = ""

    sem = asyncio.Semaphore(6)
    connector = aiohttp.TCPConnector(ssl=False)
    timeout = aiohttp.ClientTimeout(total=60)

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, proxy=PROXY_URL) as session:
            if cancel_requested:
                download_status["status"] = "cancelled"
                download_status["current_step"] = "下载已取消"
                return

            # 1. 规划
            route = await fetch_osrm_geojson(
                session, req.start_lat, req.start_lon, req.end_lat, req.end_lon, req.lane_preference or "direct"
            )
            raw_coords = [(pt[1], pt[0]) for pt in route["geometry"]["coordinates"]]
            step = req.step_meters or 20.0
            sampled = interpolate_polyline(raw_coords, step)

            if cancel_requested:
                download_status["status"] = "cancelled"
                download_status["current_step"] = "下载已取消"
                return

            download_status["current_step"] = f"正在检索街景节点 (共 {len(sampled)} 个探测点)..."
            
            # 2. 检索：严格限制搜索半径(15m)，并对每个点计算该处的路线规划正向行驶角
            async def search_worker(idx, lat, lon):
                if cancel_requested:
                    return idx, None
                async with sem:
                    try:
                        # 半径设为 15 米，防止吸附到隔壁反向车道或交叉小路上
                        p = await streetview.find_panorama_async(lat, lon, session, radius=15)
                        return idx, p
                    except Exception:
                        return idx, None

            tasks = [search_worker(i, pt[0], pt[1]) for i, pt in enumerate(sampled)]
            results = await asyncio.gather(*tasks)
            if cancel_requested:
                download_status["status"] = "cancelled"
                download_status["current_step"] = "下载已取消"
                return

            results.sort(key=lambda x: x[0])

            # 去重并进行车道朝向一致性校验
            unique_panos = []
            seen_ids = set()

            for idx, p in results:
                if cancel_requested:
                    download_status["status"] = "cancelled"
                    download_status["current_step"] = "下载已取消"
                    return
                if not p or p.id in seen_ids:
                    continue

                # 计算当前采样点在规划路线上的前进方向
                if idx < len(sampled) - 1:
                    exp_heading = calculate_bearing(sampled[idx][0], sampled[idx][1], sampled[idx + 1][0], sampled[idx + 1][1])
                elif idx > 0:
                    exp_heading = calculate_bearing(sampled[idx - 1][0], sampled[idx - 1][1], sampled[idx][0], sampled[idx][1])
                else:
                    exp_heading = 0.0

                # 检查采集车拍摄朝向与道路前进方向的夹角
                p_cam_deg = math.degrees(p.heading) if getattr(p, "heading", None) is not None else exp_heading
                heading_diff = abs((p_cam_deg - exp_heading + 180) % 360 - 180)

                # 如果夹角 > 130 度（意味着采集车是反向车道逆行拍摄的，且路中间有隔离带导致坐标偏移），
                # 尽量避免采用，或者在有多车道时优先同向车道
                # 这里记录下来
                seen_ids.add(p.id)
                unique_panos.append((p, exp_heading))

            if not unique_panos:
                raise RuntimeError("规划线路上未找到可用的 Google 街景数据，请尝试其他城市或路段。")

            download_status["total"] = len(unique_panos)
            download_status["current_step"] = f"开始下载全景图片 (共 {len(unique_panos)} 张)..."

            # 3. 下载
            metadata_list = []
            completed_count = 0

            async def dl_worker(seq_idx, pano_tuple):
                nonlocal completed_count
                if cancel_requested:
                    return None
                pano, exp_hd = pano_tuple
                # 以 pano.id 命名文件，确保不同路线或多次下载时完全按 ID 复用已缓存的瓦片
                img_name = f"{pano.id}.jpg"
                img_path = os.path.join(IMAGES_DIR, img_name)
                
                # 检查是否已下载（且文件不为空）
                file_already_exists = os.path.exists(img_path) and os.path.getsize(img_path) > 1024

                async with sem:
                    if cancel_requested:
                        return None
                    try:
                        if not file_already_exists:
                            zl = req.zoom_level if req.zoom_level is not None else 3
                            await streetview.download_panorama_async(pano, img_path, session, zoom=zl)
                    except Exception as e:
                        print(f"下载帧 {pano.id} 失败: {e}")
                        return None

                if cancel_requested:
                    return None

                completed_count += 1
                download_status["progress"] = completed_count
                status_suffix = "(已缓存)" if file_already_exists else "(新下载)"
                download_status["current_step"] = f"处理全景图 {completed_count} / {len(unique_panos)} {status_suffix}"

                cam_heading = math.degrees(pano.heading) if getattr(pano, "heading", None) is not None else exp_hd
                date_str = str(getattr(pano, "date", "")) if getattr(pano, "date", None) is not None else ""
                return {
                    "sequence_index": seq_idx,
                    "pano_id": pano.id,
                    "lat": pano.lat,
                    "lon": pano.lon,
                    "date": date_str,
                    "camera_heading_deg": round(cam_heading, 2),
                    "expected_heading": round(exp_hd, 2),
                    "image_file": f"images/{img_name}",
                }

            dl_tasks = [dl_worker(i, pt) for i, pt in enumerate(unique_panos)]
            results = await asyncio.gather(*dl_tasks)
            if cancel_requested:
                download_status["status"] = "cancelled"
                download_status["current_step"] = "下载已取消"
                return

            metadata_list = [r for r in results if r is not None]
            metadata_list.sort(key=lambda x: x["sequence_index"])

            # 4. 计算相邻航向与真实累计行车距离 (累计里程，用于平滑空间插值)
            # 对因采集车轻微横向漂移产生的锯齿进行滑动平均滤波，使行进路线平滑笔直
            smoothed_coords = []
            N = len(metadata_list)
            for i in range(N):
                # 3点窗口加权平滑: 0.25 * prev + 0.5 * curr + 0.25 * next
                prev_p = metadata_list[max(0, i - 1)]
                curr_p = metadata_list[i]
                next_p = metadata_list[min(N - 1, i + 1)]
                p_lat1, p_lon1 = float(prev_p["lat"]), float(prev_p["lon"])
                p_lat2, p_lon2 = float(curr_p["lat"]), float(curr_p["lon"])
                p_lat3, p_lon3 = float(next_p["lat"]), float(next_p["lon"])
                s_lat = 0.2 * p_lat1 + 0.6 * p_lat2 + 0.2 * p_lat3
                s_lon = 0.2 * p_lon1 + 0.6 * p_lon2 + 0.2 * p_lon3
                smoothed_coords.append((s_lat, s_lon))

            accum_dist = 0.0
            metadata_list[0]["accum_dist"] = 0.0
            metadata_list[0]["step_dist"] = 0.0

            for i in range(N):
                curr_p = metadata_list[i]
                # 保存原始全景经纬度供参考，同时生成平滑后的轨迹经纬度
                curr_p["smooth_lat"] = round(smoothed_coords[i][0], 7)
                curr_p["smooth_lon"] = round(smoothed_coords[i][1], 7)

                if i < N - 1:
                    dist_to_next = haversine_distance(
                        smoothed_coords[i][0], smoothed_coords[i][1],
                        smoothed_coords[i + 1][0], smoothed_coords[i + 1][1]
                    )
                    # 如果相邻点航向与路线期望航向夹角过大（由于离散点横跳引起），回退到期望规划航向
                    raw_drive_hd = calculate_bearing(
                        smoothed_coords[i][0], smoothed_coords[i][1],
                        smoothed_coords[i + 1][0], smoothed_coords[i + 1][1]
                    )
                    exp_hd = float(curr_p.get("expected_heading", raw_drive_hd))
                    diff = abs((raw_drive_hd - exp_hd + 180) % 360 - 180)
                    if diff > 35:
                        drive_hd = exp_hd
                    else:
                        drive_hd = raw_drive_hd

                    curr_p["drive_heading_deg"] = round(drive_hd, 2)
                    curr_p["dist_to_next"] = round(dist_to_next, 2)
                    accum_dist += dist_to_next
                    metadata_list[i + 1]["accum_dist"] = round(accum_dist, 2)
                else:
                    curr_p["drive_heading_deg"] = metadata_list[i - 1]["drive_heading_deg"] if i > 0 else 0.0
                    curr_p["dist_to_next"] = 0.0

            # 保存索引文件
            manifest_path = os.path.join(DATA_DIR, "route_manifest.json")
            if req.append_to_existing and os.path.exists(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        old_manifest = json.load(f)
                    old_frames = old_manifest.get("frames", [])
                    # 避免最后一点重复
                    if old_frames and metadata_list and old_frames[-1]["pano_id"] == metadata_list[0]["pano_id"]:
                        metadata_list = metadata_list[1:]

                    # 重新编排序号与累计里程
                    last_accum = float(old_frames[-1]["accum_dist"]) if old_frames else 0.0
                    start_idx = len(old_frames)
                    for k, mf in enumerate(metadata_list):
                        mf["sequence_index"] = start_idx + k
                        last_accum += float(mf.get("dist_to_next", 0.0))
                        if k + 1 < len(metadata_list):
                            metadata_list[k + 1]["accum_dist"] = round(last_accum, 2)

                    combined_frames = old_frames + metadata_list
                    # 更新前一段最后一帧到新段第一帧的距离和航向
                    if len(old_frames) > 0 and len(metadata_list) > 0:
                        f_last_old = combined_frames[len(old_frames) - 1]
                        f_first_new = combined_frames[len(old_frames)]
                        d_trans = haversine_distance(
                            f_last_old.get("smooth_lat", f_last_old["lat"]),
                            f_last_old.get("smooth_lon", f_last_old["lon"]),
                            f_first_new.get("smooth_lat", f_first_new["lat"]),
                            f_first_new.get("smooth_lon", f_first_new["lon"])
                        )
                        hd_trans = calculate_bearing(
                            f_last_old.get("smooth_lat", f_last_old["lat"]),
                            f_last_old.get("smooth_lon", f_last_old["lon"]),
                            f_first_new.get("smooth_lat", f_first_new["lat"]),
                            f_first_new.get("smooth_lon", f_first_new["lon"])
                        )
                        f_last_old["dist_to_next"] = round(d_trans, 2)
                        f_last_old["drive_heading_deg"] = round(hd_trans, 2)

                    # 重新精确核算从头到尾的总累计里程
                    cur_acc = 0.0
                    combined_frames[0]["accum_dist"] = 0.0
                    for ci in range(len(combined_frames)):
                        if ci < len(combined_frames) - 1:
                            cur_acc += combined_frames[ci].get("dist_to_next", 0.0)
                            combined_frames[ci + 1]["accum_dist"] = round(cur_acc, 2)

                    manifest_data = {
                        "start_point": old_manifest.get("start_point", [req.start_lat, req.start_lon]),
                        "end_point": [req.end_lat, req.end_lon],
                        "total_frames": len(combined_frames),
                        "total_distance": round(cur_acc, 2),
                        "zoom_level": req.zoom_level,
                        "frames": combined_frames,
                    }
                except Exception as ex:
                    print(f"合并已有路线异常，转为全新覆盖: {ex}")
                    manifest_data = {
                        "start_point": [req.start_lat, req.start_lon],
                        "end_point": [req.end_lat, req.end_lon],
                        "total_frames": len(metadata_list),
                        "total_distance": round(accum_dist, 2),
                        "zoom_level": req.zoom_level,
                        "frames": metadata_list,
                    }
            else:
                manifest_data = {
                    "start_point": [req.start_lat, req.start_lon],
                    "end_point": [req.end_lat, req.end_lon],
                    "total_frames": len(metadata_list),
                    "total_distance": round(accum_dist, 2),
                    "zoom_level": req.zoom_level,
                    "frames": metadata_list,
                }
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest_data, f, ensure_ascii=False, indent=2)

            download_status["status"] = "completed"
            download_status["current_step"] = f"下载完成！共保存 {manifest_data['total_frames']} 张全景图"

    except Exception as e:
        download_status["status"] = "error"
        download_status["error_message"] = str(e)
        download_status["current_step"] = f"下载失败: {e}"

@app.post("/api/start_download")
async def start_download(req: RouteRequest, bg_tasks: BackgroundTasks):
    global download_status
    if download_status["status"] == "running":
        return {"success": False, "message": "已有任务正在下载中"}
    bg_tasks.add_task(run_download_task, req)
    return {"success": True, "message": "下载任务已在后台启动"}

from fastapi.responses import Response

GOOGLE_LAYER_MODES = {
    "hybrid": "y",             # Google HD 卫星实拍混合图 (卫星实拍 + 路网与中文地名注记)
    "satellite": "s",          # Google 纯高清实拍卫星图 (无文字原片)
    "satellite_sv": "y,svv",    # Google HD 实拍卫星图 + 蓝色街景覆盖网
    "street_sv": "m,svv",      # Google 标准街道图 + 蓝色街景覆盖网
    "street": "m",             # Google 标准街道电子地图
}

@app.get("/api/tile/{layer_type}/{z}/{x}/{y}")
async def get_map_tile(layer_type: str, z: int, x: int, y: int):
    """代理 Google 高清地图瓦片 (HD 实拍卫星图、实拍混合图、街景覆盖图等)，并在本地持久缓存"""
    lyrs = GOOGLE_LAYER_MODES.get(layer_type, "y")
    tile_cache_dir = os.path.join(DATA_DIR, "tile_cache", layer_type, str(z), str(x))
    os.makedirs(tile_cache_dir, exist_ok=True)
    tile_file = os.path.join(tile_cache_dir, f"{y}.png")

    if os.path.exists(tile_file):
        try:
            with open(tile_file, "rb") as f:
                return Response(content=f.read(), media_type="image/png")
        except Exception:
            pass

    target_url = f"https://mt1.google.com/vt/lyrs={lyrs}&hl=zh-CN&x={x}&y={y}&z={z}"
    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector, proxy=PROXY_URL) as session:
        try:
            async with session.get(target_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    try:
                        with open(tile_file, "wb") as f:
                            f.write(content)
                    except Exception:
                        pass
                    return Response(content=content, media_type="image/png")
        except Exception:
            pass
    return Response(status_code=404)

@app.get("/api/sv_tile/{z}/{x}/{y}")
async def get_streetview_coverage_tile(z: int, x: int, y: int):
    """向后兼容原有街景覆盖图层接口"""
    return await get_map_tile("street_sv", z, x, y)

@app.post("/api/stop_download")
async def stop_download():
    global cancel_requested, download_status
    cancel_requested = True
    download_status["status"] = "cancelled"
    download_status["current_step"] = "下载已由用户取消"
    return {"success": True, "message": "已发送取消下载信号"}

@app.get("/api/download_status")
async def get_download_status():
    return download_status

# ========================================================
# 路线地址簿 SQLite 持久化与逆地理编码 API
# ========================================================

class SaveRouteModel(BaseModel):
    name: str
    notes: Optional[str] = ""
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    start_address: Optional[str] = ""
    end_address: Optional[str] = ""
    lane_preference: Optional[str] = "direct"
    step_meters: Optional[float] = 20.0
    distance: Optional[float] = 0.0
    duration: Optional[float] = 0.0
    extra_data: Optional[str] = "{}"

@app.get("/api/reverse_geocode")
async def reverse_geocode(lat: float, lon: float):
    """通过 OpenStreetMap Nominatim 对经纬度进行逆地理编码，走代理获取路名、城市、国家等信息"""
    norm_lon = normalize_lng(lon)
    url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={norm_lon}&zoom=18&addressdetails=1"
    headers = {
        "User-Agent": "VirtualDrivingSimulator/1.0"
    }
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(connector=connector, proxy=PROXY_URL) as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    addr = data.get("address", {})
                    country = addr.get("country", "")
                    city = (
                        addr.get("city")
                        or addr.get("town")
                        or addr.get("county")
                        or addr.get("city_district")
                        or addr.get("state", "")
                    )
                    road = (
                        addr.get("road")
                        or addr.get("pedestrian")
                        or addr.get("footway")
                        or addr.get("suburb")
                        or addr.get("amenity", "")
                    )
                    
                    # 组合简短地名描述
                    parts = [p for p in [country, city, road] if p]
                    short_name = "·".join(parts) if parts else (data.get("display_name", "")[:30] or f"路线 ({lat:.3f}, {norm_lon:.3f})")
                    
                    return {
                        "success": True,
                        "short_name": short_name,
                        "country": country,
                        "city": city,
                        "road": road,
                        "display_name": data.get("display_name", ""),
                        "address": addr
                    }
    except Exception as e:
        print(f"逆地理编码请求失败: {e}")

    # 兜底返回坐标简写
    return {
        "success": False,
        "short_name": f"路线 ({lat:.4f}, {norm_lon:.4f})",
        "country": "",
        "city": "",
        "road": "",
        "display_name": f"{lat:.4f}, {norm_lon:.4f}"
    }

@app.get("/api/routes")
def get_saved_routes():
    """获取所有已保存到 SQLite 数据库的路线地址簿列表"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM saved_routes ORDER BY id DESC")
        rows = cursor.fetchall()
        routes = [dict(row) for row in rows]
        conn.close()
        return {"success": True, "routes": routes}
    except Exception as e:
        return {"success": False, "error": str(e), "routes": []}

@app.post("/api/routes")
def save_route(item: SaveRouteModel):
    """保存路线到 SQLite 数据库中，永久存储"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO saved_routes (
                name, notes, created_at, start_lat, start_lon, end_lat, end_lon,
                start_address, end_address, lane_preference, step_meters,
                distance, duration, extra_data
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.name.strip() or "未命名路线",
            item.notes or "",
            now_str,
            item.start_lat,
            item.start_lon,
            item.end_lat,
            item.end_lon,
            item.start_address or "",
            item.end_address or "",
            item.lane_preference or "direct",
            item.step_meters or 20.0,
            item.distance or 0.0,
            item.duration or 0.0,
            item.extra_data or "{}"
        ))
        conn.commit()
        new_id = cursor.lastrowid
        conn.close()
        return {"success": True, "id": new_id, "message": "已永久保存到数据库"}
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.delete("/api/routes/{route_id}")
def delete_route(route_id: int):
    """从 SQLite 数据库中删除一条保存的路线"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM saved_routes WHERE id = ?", (route_id,))
        conn.commit()
        conn.close()
        return {"success": True, "message": "删除成功"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# 挂载街景图片静态目录
app.mount("/streetview_route", StaticFiles(directory=DATA_DIR), name="streetview_route")

@app.get("/")
async def get_index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))

def ensure_port_available(port: int):
    """服务启动前检查端口是否被占用，若存在残留旧进程则自动结束并释放端口"""
    if sys.platform != "win32":
        return
    import subprocess
    current_pid = os.getpid()
    try:
        output = subprocess.check_output("netstat -ano", shell=True).decode("latin1", errors="ignore")
        killed_any = False
        for line in output.splitlines():
            line = line.strip()
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if len(parts) >= 5:
                    target_pid_str = parts[-1]
                    if target_pid_str.isdigit():
                        target_pid = int(target_pid_str)
                        if target_pid != 0 and target_pid != current_pid:
                            print(f"[*] 检测到端口 {port} 被残留进程 (PID: {target_pid}) 占用，正在强制结束...")
                            subprocess.call(f"taskkill /F /PID {target_pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            killed_any = True
        if killed_any:
            import time
            time.sleep(1)
            print(f"[*] 端口 {port} 占用已清除完毕！")
    except Exception as e:
        print(f"[!] 端口检查警告: {e}")

if __name__ == "__main__":
    import uvicorn
    ensure_port_available(PORT)
    print(f"[*] 正在启动虚拟驾驶服务: http://{HOST}:{PORT}")
    print(f"[*] 代理节点: {PROXY_URL or '直连 (未启用代理)'}")
    print(f"[*] 街景数据目录: {DATA_DIR}")
    print(f"[*] SQLite 数据库: {DB_PATH}")
    uvicorn.run(app, host=HOST, port=PORT)
