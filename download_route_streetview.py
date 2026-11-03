import os
import json
import math
import asyncio
import aiohttp
from streetlevel import streetview

# 加载 .env 环境变量
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path, override=True)
    except Exception:
        with open(_env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v

# ==================== 核心配置参数 ====================
# 示例坐标：旧金山九曲花街周边路段 (格式: 纬度, 经度)
START_POINT = (37.802139, -122.418740)
END_POINT = (37.801055, -122.426092)

SAMPLE_STEP_METERS = 10.0          # 沿路线每隔多少米探测一次街景
ZOOM_LEVEL = 3                     # 画质等级: 0(最低) ~ 5(最高), 3 为 4096x2048 性价比最高
MAX_CONCURRENCY = 5                # 异步并发协程数（建议 5~8，防止触发 Google 频控）
OUTPUT_DIR = os.getenv("DATA_DIR", "./streetview_route")  # 本地保存目录

# 网络代理设置（优先从 .env 读取，留空则为 None 直连）
_env_proxy = os.getenv("PROXY_URL", "").strip()
PROXY_URL = _env_proxy if _env_proxy else None
# ======================================================


def haversine_distance(lat1, lon1, lat2, lon2):
    """计算两点间的地球表面距离（米）"""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * R * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def calculate_bearing(lat1, lon1, lat2, lon2):
    """计算从点1驶向点2的行车正北航向角 (0~360度)"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def interpolate_polyline(coords, step_meters=10.0):
    """对路线折线进行等距插值，防止长直道缺少中间采样点"""
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


async def fetch_osrm_route(session, start, end):
    """使用开源 OSRM 接口免费规划驾车路径（免 API Key）"""
    # 注意 OSRM URL 顺序为: 经度,纬度
    url = (
        f"http://router.project-osrm.org/route/v1/driving/"
        f"{start[1]},{start[0]};{end[1]},{end[0]}"
        f"?overview=full&geometries=geojson"
    )
    print("[1/4] 正在请求 OSRM 规划驾车路线...")
    async with session.get(url) as resp:
        data = await resp.json()
        if data.get("code") != "Ok":
            raise RuntimeError(f"路径规划失败: {data}")
        # 将 GeoJSON 的 [lon, lat] 转换为 (lat, lon)
        raw_coords = [(pt[1], pt[0]) for pt in data["routes"][0]["geometry"]["coordinates"]]
        distance = data["routes"][0]["distance"]
        print(f"      路线总长: {distance:.1f} 米，包含 {len(raw_coords)} 个原始折点。")
        return raw_coords


async def find_pano_worker(idx, lat, lon, session, sem):
    """根据采样点查找最近的 Google 街景 Panorama 元数据"""
    async with sem:
        try:
            pano = await streetview.find_panorama_async(
                lat, lon, session, radius=30
            )
            return idx, pano
        except Exception as e:
            print(f"  [!] 采样点 #{idx} ({lat:.5f}, {lon:.5f}) 检索异常: {e}")
            return idx, None


async def download_pano_worker(seq_idx, pano, output_dir, session, sem):
    """下载拼接好的等距柱状全景大图并提取元数据"""
    img_filename = f"{seq_idx:04d}_{pano.id}.jpg"
    img_path = os.path.join(output_dir, "images", img_filename)

    async with sem:
        try:
            if not os.path.exists(img_path):
                await streetview.download_panorama_async(
                    pano, img_path, session, zoom=ZOOM_LEVEL
                )
                print(f"  [+] 已下载帧 #{seq_idx:04d} | PanoID: {pano.id} | 坐标: ({pano.lat:.5f}, {pano.lon:.5f})")
            else:
                print(f"  [*] 已存在帧 #{seq_idx:04d} | PanoID: {pano.id} (跳过下载)")
        except Exception as e:
            print(f"  [!] 下载全景图 {pano.id} 失败: {e}")
            return None

    # 提取拍摄采集车朝向（弧度转角度）
    car_heading = math.degrees(pano.heading) if getattr(pano, "heading", None) is not None else 0.0

    # 提取相邻街景拓扑节点（供前端自由变道或路口转向使用）
    neighbors = []
    if getattr(pano, "neighbors", None):
        neighbors = [{"id": n.id, "lat": n.lat, "lon": n.lon} for n in pano.neighbors]

    return {
        "sequence_index": seq_idx,
        "pano_id": pano.id,
        "lat": pano.lat,
        "lon": pano.lon,
        "date": str(getattr(pano, "date", "")),
        "camera_heading_deg": round(car_heading, 2),
        "image_file": f"images/{img_filename}",
        "neighbors": neighbors,
    }


async def main():
    os.makedirs(os.path.join(OUTPUT_DIR, "images"), exist_ok=True)
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    # 配置连接池与超时时间
    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector, proxy=PROXY_URL) as session:
        # 1. 规划路线并等距插值
        raw_coords = await fetch_osrm_route(session, START_POINT, END_POINT)
        sampled_points = interpolate_polyline(raw_coords, step_meters=SAMPLE_STEP_METERS)
        print(f"[2/4] 按每 {SAMPLE_STEP_METERS} 米插值完毕，共生成 {len(sampled_points)} 个探测坐标点。")

        # 2. 批量并发检索街景节点
        print("[3/4] 正在并发检索路线上的街景节点元数据...")
        find_tasks = [
            find_pano_worker(i, lat, lon, session, sem)
            for i, (lat, lon) in enumerate(sampled_points)
        ]
        results = await asyncio.gather(*find_tasks)
        results.sort(key=lambda x: x[0])

        # 按行驶顺序对 Pano ID 去重（避免多个密集采样点命中同一个街景球）
        unique_panos = []
        seen_ids = set()
        for _, pano in results:
            if pano and pano.id not in seen_ids:
                seen_ids.add(pano.id)
                unique_panos.append(pano)

        print(f"      共匹配到 {len(unique_panos)} 个唯一连续街景节点。")

        # 3. 批量并发下载全景大图
        print(f"[4/4] 开始并发下载全景图 (Zoom={ZOOM_LEVEL})...")
        dl_tasks = [
            download_pano_worker(idx, pano, OUTPUT_DIR, session, sem)
            for idx, pano in enumerate(unique_panos)
        ]
        metadata_list = await asyncio.gather(*dl_tasks)
        metadata_list = [m for m in metadata_list if m is not None]
        metadata_list.sort(key=lambda x: x["sequence_index"])

        # 4. 计算每一帧驶向下一帧的真实道路行驶朝向 (drive_heading_deg)
        for i in range(len(metadata_list)):
            if i < len(metadata_list) - 1:
                curr_p, next_p = metadata_list[i], metadata_list[i + 1]
                drive_heading = calculate_bearing(
                    curr_p["lat"], curr_p["lon"], next_p["lat"], next_p["lon"]
                )
                curr_p["drive_heading_deg"] = round(drive_heading, 2)
                curr_p["next_pano_id"] = next_p["pano_id"]
            else:
                metadata_list[i]["drive_heading_deg"] = (
                    metadata_list[i - 1]["drive_heading_deg"] if i > 0 else 0.0
                )
                metadata_list[i]["next_pano_id"] = None

        # 5. 保存路线清单 JSON
        manifest_path = os.path.join(OUTPUT_DIR, "route_manifest.json")
        manifest_data = {
            "start_point": START_POINT,
            "end_point": END_POINT,
            "total_frames": len(metadata_list),
            "zoom_level": ZOOM_LEVEL,
            "frames": metadata_list,
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, ensure_ascii=False, indent=2)

        print(f"\n[完成] 共保存 {len(metadata_list)} 张全景图！")
        print(f"       路线元数据索引文件: {manifest_path}")


if __name__ == "__main__":
    # Windows 平台下避免 ProactorEventLoop 退出时报错
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
