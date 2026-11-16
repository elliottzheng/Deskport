# -*- coding: utf-8 -*-
"""工作项（批次 G2 流水线）CLI：wi new / ls / show / decide。

主管派**开发类**工作必须走工作项（平台强制：建 worktree、派实施、派验收、
返工回同一会话、集成门禁全自动）；直接建任务只用于调查类。入口在
deskport.py（`python deskport.py wi ...` 转到这里），复用同一个 Deskport
客户端与 .deskport.json 配置。
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from deskport import Deskport, DeskportError  # noqa: E402


def _client(a):
    cfg = {}
    cfgfile = HERE / ".deskport.json"
    if cfgfile.exists():
        try:
            cfg = json.loads(cfgfile.read_text(encoding="utf-8"))
        except ValueError:
            cfg = {}
    return Deskport(a.url or cfg.get("url"),
                    insecure=a.insecure if a.insecure is not None else bool(cfg.get("insecure")),
                    cafile=cfg.get("cafile"))


def _request(dp, method, path, body=None):
    return dp._request(method, path, body)


STAGE_TEXT = {'impl': '实施中', 'verify': '验收中', 'integrate': '集成中',
              'queue': '排队中', 'done': '已集成', 'judge': '待判断',
              'fail': '失败', 'cancelled': '已取消'}


def _row(w):
    verdict = w.get("lastVerdict") or ""
    bits = [STAGE_TEXT.get(w.get("stage"), w.get("stage") or "")]
    bits.append("attempt %d/%d" % (w.get("attempt") or 0, w.get("maxAttempts") or 0))
    if verdict:
        bits.append("verdict=%s" % verdict)
    if w.get("state") == "queued" and w.get("queuedFor"):
        share = "、".join(w.get("queuedShare") or []) or "足迹"
        bits.append("等 %s（共享 %s）" % (w["queuedFor"][:8], share))
    if w.get("state") == "needs_judgment":
        bits.append("待判断：%s" % (w.get("judgmentReason") or "")[:60])
    return "%s  %-28s %s" % (w.get("id"), (w.get("title") or "")[:28],
                             " · ".join(bits))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="deskport.py wi",
                                 description="工作项（实施→验收→返工→集成）")
    ap.add_argument("--url")
    ap.add_argument("-k", "--insecure", action="store_true", default=None)
    sub = ap.add_subparsers(dest="wi_cmd", required=True)

    p = sub.add_parser("new", help="建工作项并派实施（开发类活必须走这里）")
    p.add_argument("--project", required=True, help="项目 id（GET /api/settings/projects）")
    p.add_argument("--spec", required=True, metavar="文件", help="规格文件路径")
    p.add_argument("--impl", required=True, help="实施模型")
    p.add_argument("--verify", required=True, help="验收模型（必须与实施不同）")
    p.add_argument("--title", help="标题（≤40 字，缺省取规格文件名）")
    p.add_argument("--footprint", nargs="+", required=True, metavar="GLOB",
                   help="预计改动的文件/目录（glob，至少一项；用于防冲突调度）")
    p.add_argument("--force-parallel", action="store_true",
                   help="足迹相交也并行（记 history）")

    sub.add_parser("ls", help="列工作项（一行一个）")

    p = sub.add_parser("show", help="工作项详情")
    p.add_argument("wid")

    p = sub.add_parser("decide", help="主管判断（needs_judgment 时）")
    p.add_argument("wid")
    p.add_argument("action", choices=["continue", "rework", "integrate", "cancel"])
    p.add_argument("--text", help="继续的说明 / 返工要点")

    a = ap.parse_args(argv)
    dp = _client(a)
    try:
        if a.wi_cmd == "new":
            body = {"projectId": a.project, "specPath": a.spec,
                    "implModel": a.impl, "verifyModel": a.verify,
                    "footprint": a.footprint,
                    "forceParallel": a.force_parallel}
            if a.title:
                body["title"] = a.title
            out = _request(dp, "POST", "/api/workitems", body)
            w = out.get("workItem") or {}
            print("%s  %s  %s" % (w.get("id"), w.get("title") or "",
                                  STAGE_TEXT.get(w.get("stage"), "")))
            if w.get("state") == "queued":
                print("已排队：等 %s（共享 %s）"
                      % (w.get("queuedFor") or "?", "、".join(w.get("queuedShare") or [])))
            return 0
        if a.wi_cmd == "ls":
            for w in _request(dp, "GET", "/api/workitems").get("workItems") or []:
                print(_row(w))
            return 0
        if a.wi_cmd == "show":
            w = _request(dp, "GET", "/api/workitems/%s" % a.wid).get("workItem") or {}
            print(json.dumps(w, ensure_ascii=False, indent=1))
            return 0
        if a.wi_cmd == "decide":
            out = _request(dp, "POST", "/api/workitems/%s/decision" % a.wid,
                           {"action": a.action, "text": a.text or ""})
            w = out.get("workItem") or {}
            print("%s -> %s（%s）" % (a.wid, w.get("state") or "",
                                     STAGE_TEXT.get(w.get("stage"), "")))
            return 0
    except DeskportError as e:
        print("wi: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
