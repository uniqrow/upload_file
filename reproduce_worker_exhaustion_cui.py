#!/usr/bin/env python3
"""
========================================================
Apache Worker枯渇 再現スクリプト
========================================================
事象:
  - ESTABLISHED数は5〜15程度なのに
  - 1時間後にBusyWorkersが258(MaxRequestWorkers)を超える

原因:
  KeepAliveTimeout(5秒)でTCP接続は切れるはずが、
  WAF/FWがTCP keepaliveパケットを送り続けることで
  ApacheのTimeout(3600秒)がカウントされworkerが占有され続ける

再現戦略:
  --keepalive-time 3600 のcurl相当の接続を
  1秒ごとに新規追加し続けてworkerの蓄積を観測する

使い方:
  python3 reproduce_worker_exhaustion.py --host 192.168.1.10 --vhost vhost1.example.com
  python3 reproduce_worker_exhaustion.py --host 192.168.1.10 --vhost vhost1.example.com --duration 3600 --interval 1
  python3 reproduce_worker_exhaustion.py --host 192.168.1.10 --vhost vhost1.example.com --mode fast  # 短時間で枯渇を確認
"""

import argparse
import socket
import threading
import time
import sys
import signal
import urllib.request
import urllib.error
from datetime import datetime
from collections import defaultdict


# ============================================================
# グローバル状態
# ============================================================
active_connections = []
active_connections_lock = threading.Lock()
stop_event = threading.Event()
stats = defaultdict(int)
stats_lock = threading.Lock()
log_lines = []


# ============================================================
# ユーティリティ
# ============================================================
def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    log_lines.append(line)


def log_stat(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    log_lines.append(line)


# ============================================================
# コネクション保持スレッド
# ============================================================
class KeepAliveConnection(threading.Thread):
    """
    HTTPリクエストを1回送った後、接続を切らずに保持し続けるスレッド。
    WAF/FWがApacheへのTCP接続をプールしている状態を模擬する。
    """

    def __init__(self, host, port, vhost, conn_id, keepalive_sec=3600):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.vhost = vhost
        self.conn_id = conn_id
        self.keepalive_sec = keepalive_sec
        self.sock = None
        self.connected_at = None
        self.status = "init"

    def run(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)

            # TCP keepalive を有効化（OSレベル）
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                # keepalive開始まで60秒、間隔10秒、リトライ6回
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 6)
            except AttributeError:
                pass  # macOSなど非対応OSはスキップ

            self.sock.connect((self.host, self.port))
            self.connected_at = time.time()
            self.status = "connected"

            # HTTP/1.1 GETリクエスト送信（Connection: keep-alive）
            request = (
                f"GET / HTTP/1.1\r\n"
                f"Host: {self.vhost}\r\n"
                f"Connection: keep-alive\r\n"
                f"User-Agent: WorkerExhaustionTest/1.0 (conn_id={self.conn_id})\r\n"
                f"\r\n"
            )
            self.sock.sendall(request.encode())

            # レスポンスのヘッダ部分だけ受け取る
            self.sock.settimeout(15)
            response = b""
            try:
                while b"\r\n\r\n" not in response:
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        break
                    response += chunk
            except socket.timeout:
                pass  # ヘッダが来なくてもOK（接続を保持するのが目的）

            self.status = "holding"  # 接続保持中

            with stats_lock:
                stats["connected"] += 1

            # ここが核心: 接続を切らずに保持し続ける
            # → ApacheのworkerはKeepAliveTimeoutを待つが、
            #   TCP keepaliveパケットにより切断されず3600秒占有される
            deadline = time.time() + self.keepalive_sec
            while not stop_event.is_set() and time.time() < deadline:
                try:
                    # 定期的にTCP keepaliveパケットを送信（接続を維持）
                    # 実際のWAFのコネクションプール動作を模擬
                    self.sock.settimeout(30)
                    time.sleep(30)
                    # 空のデータを送ってTCPレベルで接続を維持
                    # （アプリレベルでは何も送らない = KeepAliveTimeoutをリセットしない）
                except Exception:
                    break

        except ConnectionRefusedError:
            self.status = "refused"
            with stats_lock:
                stats["refused"] += 1
            log(f"接続拒否 conn_id={self.conn_id} (MaxRequestWorkers到達の可能性)", "WARN")

        except socket.timeout:
            self.status = "timeout"
            with stats_lock:
                stats["timeout"] += 1
            log(f"接続タイムアウト conn_id={self.conn_id}", "WARN")

        except Exception as e:
            self.status = "error"
            with stats_lock:
                stats["error"] += 1

        finally:
            self.status = "closed"
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
            with stats_lock:
                stats["connected"] = max(0, stats["connected"] - 1)

    def close(self):
        self.sock and self.sock.close()

    def held_seconds(self):
        if self.connected_at:
            return int(time.time() - self.connected_at)
        return 0


# ============================================================
# Apache server-status 取得
# ============================================================
def fetch_server_status(host, port=80):
    """
    http://host/server-status?auto からBusyWorkers/IdleWorkersを取得。
    mod_statusが有効かつ127.0.0.1からアクセス許可が必要。
    取得できない場合はNoneを返す。
    """
    try:
        url = f"http://{host}:{port}/server-status?auto"
        req = urllib.request.Request(url, headers={"Host": "localhost"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read().decode()
        result = {}
        for line in body.splitlines():
            if ": " in line:
                k, v = line.split(": ", 1)
                result[k.strip()] = v.strip()
        return result
    except Exception:
        return None


# ============================================================
# TCP ESTABLISHED数を取得（/proc/net/tcp）
# ============================================================
def count_established(target_host, target_port):
    """
    /proc/net/tcp を読んでESTABLISHED(state=01)の接続数をカウント。
    Linuxのみ動作。
    """
    try:
        # ターゲットIPを16進数に変換
        parts = target_host.split(".")
        hex_ip = "{:02X}{:02X}{:02X}{:02X}".format(
            int(parts[3]), int(parts[2]), int(parts[1]), int(parts[0])
        )
        hex_port = f"{target_port:04X}"
        target = f"{hex_ip}:{hex_port}"

        count = 0
        with open("/proc/net/tcp", "r") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) < 4:
                    continue
                remote = parts[2]
                state = parts[3]
                if remote == target and state == "01":  # 01 = ESTABLISHED
                    count += 1
        return count
    except Exception:
        return -1


# ============================================================
# 監視スレッド
# ============================================================
def monitor_loop(host, port, interval=10):
    """
    定期的にApache worker状態・TCP接続数・スレッド数を出力するループ。
    """
    start_time = time.time()
    log("監視開始", "MONITOR")
    log(f"{'経過時間':>8} | {'生存接続数':>8} | {'ESTABLISHED':>11} | {'BusyWorkers':>11} | {'IdleWorkers':>11} | {'状態':>6}", "MONITOR")
    log("-" * 80, "MONITOR")

    while not stop_event.is_set():
        elapsed = int(time.time() - start_time)

        # 生存しているスレッド数
        with active_connections_lock:
            holding = sum(1 for c in active_connections if c.status == "holding")
            total = len(active_connections)

        # TCP ESTABLISHED数
        est = count_established(host, port)
        est_str = str(est) if est >= 0 else "N/A(非Linux)"

        # Apache server-status
        status = fetch_server_status(host, port)
        if status:
            busy = status.get("BusyWorkers", "N/A")
            idle = status.get("IdleWorkers", "N/A")
            # 枯渇判定
            try:
                busy_int = int(busy)
                flag = " ⚠ 枯渇寸前!" if busy_int > 200 else (" ✓" if busy_int < 100 else "")
            except Exception:
                flag = ""
            state_str = f"Busy={busy}{flag}"
        else:
            busy = "N/A"
            idle = "N/A"
            state_str = "server-status取得不可"

        elapsed_str = f"{elapsed//3600:02d}:{(elapsed%3600)//60:02d}:{elapsed%60:02d}"
        log_stat(
            f"{elapsed_str:>8} | {holding:>8} | {est_str:>11} | {busy:>11} | {idle:>11} | {state_str}"
        )

        time.sleep(interval)


# ============================================================
# 接続生成ループ
# ============================================================
def spawn_connections(host, port, vhost, interval_sec, duration_sec, keepalive_sec, max_conn):
    """
    指定間隔で新規接続スレッドを生成し続ける。
    """
    start_time = time.time()
    conn_id = 0

    log(f"接続生成開始: interval={interval_sec}秒, duration={duration_sec}秒, max_conn={max_conn}", "SPAWN")

    while not stop_event.is_set():
        elapsed = time.time() - start_time
        if elapsed >= duration_sec:
            log(f"指定時間({duration_sec}秒)経過 - 接続生成を停止", "SPAWN")
            break

        with active_connections_lock:
            current = len([c for c in active_connections if c.is_alive()])

        if current >= max_conn:
            log(f"最大接続数({max_conn})に達したため待機中...", "WARN")
            time.sleep(interval_sec)
            continue

        conn = KeepAliveConnection(host, port, vhost, conn_id, keepalive_sec)
        conn.start()
        with active_connections_lock:
            active_connections.append(conn)

        conn_id += 1
        if conn_id % 10 == 0:
            log(f"接続生成数: {conn_id}件", "SPAWN")

        time.sleep(interval_sec)

    log("接続生成ループ終了", "SPAWN")


# ============================================================
# クリーンアップ
# ============================================================
def cleanup(signum=None, frame=None):
    log("終了処理中... (Ctrl+Cで強制終了)", "INFO")
    stop_event.set()

    with active_connections_lock:
        conns = list(active_connections)

    for c in conns:
        c.close()

    # 結果サマリ出力
    print("\n" + "=" * 60)
    print("【検証結果サマリ】")
    print("=" * 60)
    with stats_lock:
        print(f"  総接続試行数    : {stats['connected'] + stats['refused'] + stats['timeout'] + stats['error']}")
        print(f"  成功接続数      : {stats['connected']}")
        print(f"  接続拒否(503等) : {stats['refused']}")
        print(f"  タイムアウト    : {stats['timeout']}")
        print(f"  エラー          : {stats['error']}")
    print("=" * 60)
    print("\n[判定]")
    if stats["refused"] > 0:
        print("  ✅ MaxRequestWorkers到達による接続拒否を確認 → 事象再現成功")
    else:
        print("  ℹ️  接続拒否は未確認 - 監視ログのBusyWorkersの推移を確認してください")
    print()

    sys.exit(0)


# ============================================================
# エントリポイント
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Apache Worker枯渇 再現スクリプト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
モード説明:
  normal  : 1秒ごとに接続を追加。実際の事象(1時間後に枯渇)を再現
  fast    : 0.1秒ごとに接続を追加。短時間で枯渇を確認したい場合
  monitor : 接続生成なし。現在のApache状態を監視するだけ

使用例:
  # 基本的な再現（1時間程度かかる）
  python3 reproduce_worker_exhaustion.py --host 192.168.1.10 --vhost vhost1.example.com

  # 短時間で枯渇を確認
  python3 reproduce_worker_exhaustion.py --host 192.168.1.10 --vhost vhost1.example.com --mode fast

  # 現在の状態を監視するだけ
  python3 reproduce_worker_exhaustion.py --host 192.168.1.10 --vhost vhost1.example.com --mode monitor
        """
    )
    parser.add_argument("--host",     required=True,  help="ApacheサーバーのIPアドレス")
    parser.add_argument("--port",     type=int, default=80, help="ポート番号 (default: 80)")
    parser.add_argument("--vhost",    required=True,  help="バーチャルホスト名 (Hostヘッダ)")
    parser.add_argument("--mode",     default="normal", choices=["normal", "fast", "monitor"],
                        help="実行モード (default: normal)")
    parser.add_argument("--duration", type=int, default=7200,
                        help="接続生成を続ける秒数 (default: 7200 = 2時間)")
    parser.add_argument("--interval", type=float, default=None,
                        help="接続生成間隔(秒) ※modeより優先")
    parser.add_argument("--max-conn", type=int, default=300,
                        help="最大同時接続スレッド数 (default: 300)")
    parser.add_argument("--keepalive", type=int, default=3600,
                        help="各接続の保持時間(秒) (default: 3600)")
    parser.add_argument("--monitor-interval", type=int, default=10,
                        help="監視出力の間隔(秒) (default: 10)")
    args = parser.parse_args()

    # モード別のデフォルト間隔
    if args.interval is not None:
        interval = args.interval
    elif args.mode == "fast":
        interval = 0.1
    else:
        interval = 1.0

    # シグナルハンドラ設定
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    print("=" * 60)
    print("  Apache Worker枯渇 再現スクリプト")
    print("=" * 60)
    print(f"  対象ホスト     : {args.host}:{args.port}")
    print(f"  バーチャルホスト: {args.vhost}")
    print(f"  モード         : {args.mode}")
    print(f"  接続生成間隔   : {interval}秒")
    print(f"  接続保持時間   : {args.keepalive}秒")
    print(f"  最大接続数     : {args.max_conn}")
    print(f"  実行時間       : {args.duration}秒")
    print("=" * 60)
    print()
    print("[注意] Ctrl+C で終了・サマリ表示")
    print("[注意] server-status取得にはApacheのmod_statusが必要")
    print()

    # 監視スレッド起動
    monitor_thread = threading.Thread(
        target=monitor_loop,
        args=(args.host, args.port, args.monitor_interval),
        daemon=True
    )
    monitor_thread.start()

    if args.mode == "monitor":
        log("monitorモード: 接続生成なし。Ctrl+Cで終了。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            cleanup()
        return

    # 接続生成
    spawn_connections(
        host=args.host,
        port=args.port,
        vhost=args.vhost,
        interval_sec=interval,
        duration_sec=args.duration,
        keepalive_sec=args.keepalive,
        max_conn=args.max_conn,
    )

    # 接続生成完了後、接続が切れるまで監視継続
    log("接続生成完了。保持中の接続が切れるまで監視を継続します (Ctrl+Cで終了)")
    try:
        while True:
            with active_connections_lock:
                alive = sum(1 for c in active_connections if c.is_alive())
            if alive == 0:
                break
            time.sleep(5)
    except KeyboardInterrupt:
        pass

    cleanup()


if __name__ == "__main__":
    main()