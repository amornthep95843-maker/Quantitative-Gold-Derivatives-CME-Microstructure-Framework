import json
import urllib.request
from datetime import datetime, timezone

WORKER_URL = "https://tv-gold-options.amornthep95843.workers.dev"

def main():
    req = urllib.request.Request(
        WORKER_URL,
        headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if not data.get("ok") or not data.get("expirations"):
        raise RuntimeError("Worker ไม่ส่งข้อมูล expirations กลับมา")

    # เพิ่ม timestamp อัปเดตล่าสุด
    data["updated_by"] = "github-actions-via-worker"
    data["updated_at_utc"] = datetime.now(timezone.utc).isoformat()

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"อัปเดต data.json สำเร็จ: {len(data['expirations'])} วันหมดอายุ")

if __name__ == "__main__":
    main()
