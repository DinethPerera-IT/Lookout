"""python -m lookout.dashboard [--db PATH] [--bind 127.0.0.1] [--port 8088]"""
import argparse
import os
from pathlib import Path

from .. import config
from .server import make_server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lookout.dashboard", description="Lookout's read-only dashboard")
    parser.add_argument("--db", help="state.sqlite to read (opened read-only)")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--bind", help="address to listen on (default from config: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="port (default from config: 8088)")
    args = parser.parse_args(argv)
    cfg = config.load(args.config)
    dash = cfg.get("dashboard", {})
    db = Path(args.db or os.environ.get("LOOKOUT_DB") or
              (Path(os.environ["STATE_DIRECTORY"]) / "state.sqlite" if os.environ.get("STATE_DIRECTORY") else
               Path(cfg["state"]["default_path"]).expanduser()))
    host, port = args.bind or dash.get("bind", "127.0.0.1"), args.port or dash.get("port", 8088)
    server = make_server(db, host, port, extra_names=dash.get("hide_names", []))
    print(f"lookout dashboard: http://{host}:{port}/ (read-only{'' if db.exists() else ', no data yet: replay mode'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        server.server_close()


if __name__ == "__main__":
    main()
