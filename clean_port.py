import sys
import os
import subprocess
import time

def free_port(port: int):
    """跨平台检查并强制释放被占用的端口"""
    current_pid = os.getpid()
    try:
        if sys.platform == "win32":
            output = subprocess.check_output("netstat -ano", shell=True).decode("latin1", errors="ignore")
            killed_pids = set()
            for line in output.splitlines():
                line = line.strip()
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if len(parts) >= 5:
                        pid_str = parts[-1]
                        if pid_str.isdigit():
                            pid = int(pid_str)
                            if pid != 0 and pid != current_pid and pid not in killed_pids:
                                print(f"[*] 发现端口 {port} 被进程 (PID: {pid}) 占用，正在强制结束...")
                                subprocess.call(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                killed_pids.add(pid)
            if killed_pids:
                time.sleep(1)
                print(f"[*] 端口 {port} 已成功释放！")
            else:
                print(f"[*] 端口 {port} 状态正常 (未被占用)。")
        else:
            # Linux / macOS
            subprocess.call(f"fuser -k {port}/tcp", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[!] 端口检测提示: {e}")

if __name__ == "__main__":
    target_port = 3000
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        target_port = int(sys.argv[1])
    free_port(target_port)
