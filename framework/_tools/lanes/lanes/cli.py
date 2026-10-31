"""Command-line entry point."""

import argparse
import hashlib
import secrets
import sys
from pathlib import Path

from .config import load
from .gate import GateError, run
from .ledger import Ledger
from .router import Router
from .serve import serve
from .sync import apply, diff


def parser():
    root = argparse.ArgumentParser(prog="lane")
    root.add_argument("class_")
    root.add_argument(
        "--tier", choices=("public", "business", "business-pii", "private")
    )
    root.add_argument("--schema")
    root.add_argument("--check")
    root.add_argument("--facts")
    root.add_argument("--caller", default="interactive")
    root.add_argument("--batch", action="store_true")
    root.add_argument("--untrusted", action="store_true")
    root.add_argument("--json", action="store_true")
    sub = root.add_subparsers(dest="command")
    sub.add_parser("doctor")
    chains = sub.add_parser("chains")
    chains.add_argument("action", choices=("sync",))
    chains.add_argument("--apply", action="store_true")
    admission = sub.add_parser("admit")
    admission.add_argument("class_")
    admission.add_argument("--chain", required=True)
    admission.add_argument("--tier", required=True)
    admission.add_argument("--reason", required=True)
    admission.add_argument("--evidence", default="")
    admission.add_argument("--manual", action="store_true")
    sub.add_parser("admission")
    token = sub.add_parser("token")
    token.add_argument("action", choices=("add", "rotate", "revoke"))
    token.add_argument("caller")
    token.add_argument("--max-tier", default="business")
    server = sub.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8388)
    return root


def main():
    if len(sys.argv) > 1 and sys.argv[1] in {
        "doctor",
        "chains",
        "admit",
        "admission",
        "token",
        "serve",
    }:
        command = sys.argv[1]
        if command == "doctor":
            router = Router()
            catalog = router.catalog()
            print("router: reachable")
            print("catalog:", len(catalog))
            print(
                "cloudflare enabled:",
                sum(
                    item.get("enabled", False)
                    for item in catalog
                    if item["platform"] == "cloudflare"
                ),
            )
            print(
                "providers covered:",
                len(
                    {item["platform"] for item in catalog} & set(load("providers.yaml"))
                ),
            )
            print(
                "chains sync:", len(diff(catalog, router.profiles())), "planned changes"
            )
            return
        if command == "chains":
            command_parser = argparse.ArgumentParser(prog="lane chains")
            command_parser.add_argument("action", choices=("sync",))
            command_parser.add_argument("--apply", action="store_true")
            options = command_parser.parse_args(sys.argv[2:])
            router = Router()
            changes = diff(router.catalog(), router.profiles())
            print("APPLY" if options.apply else "DRY RUN")
            for change in changes:
                print(change["description"])
            if options.apply:
                print("statuses:", apply(router, changes))
            return
        if command == "serve":
            command_parser = argparse.ArgumentParser(prog="lane serve")
            command_parser.add_argument("--host", default="127.0.0.1")
            command_parser.add_argument("--port", type=int, default=8388)
            options = command_parser.parse_args(sys.argv[2:])
            serve(options.host, options.port)
            return
        if command == "admission":
            for row in Ledger().db.execute("SELECT * FROM admission"):
                print(dict(row))
            return
        if command == "admit":
            command_parser = argparse.ArgumentParser(prog="lane admit")
            command_parser.add_argument("class_")
            command_parser.add_argument("--chain", required=True)
            command_parser.add_argument("--tier", required=True)
            command_parser.add_argument("--reason", required=True)
            command_parser.add_argument("--evidence", default="")
            command_parser.add_argument("--schema")
            command_parser.add_argument("--manual", action="store_true")
            options = command_parser.parse_args(sys.argv[2:])
            if not options.manual:
                raise SystemExit("--manual is required")
            spec = load("classes.yaml").get(options.class_, {})
            if (
                spec.get("kind") == "checkable"
                and not spec.get("check")
                and not options.schema
            ):
                raise SystemExit("checkable class needs --schema")
            evidence = options.evidence + (
                f" schema={options.schema}" if options.schema else ""
            )
            Ledger().set_admission(
                options.class_,
                options.chain,
                options.tier,
                True,
                options.reason,
                evidence,
            )
            print("admitted")
            return
        command_parser = argparse.ArgumentParser(prog="lane token")
        command_parser.add_argument("action", choices=("add", "rotate", "revoke"))
        command_parser.add_argument("caller")
        command_parser.add_argument("--max-tier", default="business")
        token_command(command_parser.parse_args(sys.argv[2:]))
        return
    args = parser().parse_args()
    if args.command == "doctor":
        router = Router()
        catalog = router.catalog()
        print("router: reachable")
        print("catalog:", len(catalog))
        print(
            "cloudflare enabled:",
            sum(
                item.get("enabled", False)
                for item in catalog
                if item["platform"] == "cloudflare"
            ),
        )
        print(
            "providers covered:",
            len({item["platform"] for item in catalog} & set(load("providers.yaml"))),
        )
        print("chains sync:", len(diff(catalog, router.profiles())), "planned changes")
        return
    if args.command == "chains":
        router = Router()
        changes = diff(router.catalog(), router.profiles())
        print("APPLY" if args.apply else "DRY RUN")
        for change in changes:
            print(change["description"])
        if args.apply:
            print("statuses:", apply(router, changes))
        return
    if args.command == "admit":
        if not args.manual:
            raise SystemExit("--manual is required")
        Ledger().set_admission(
            args.class_, args.chain, args.tier, True, args.reason, args.evidence
        )
        print("admitted")
        return
    if args.command == "admission":
        for row in Ledger().db.execute("SELECT * FROM admission"):
            print(dict(row))
        return
    if args.command == "token":
        token_command(args)
        return
    if args.command == "serve":
        serve(args.host, args.port)
        return
    if not args.class_ or not args.tier:
        parser().error("class and --tier are required")
    try:
        output = run(
            args.class_,
            args.tier,
            sys.stdin.read(),
            args.caller,
            untrusted=args.untrusted,
            check=args.check,
            schema=args.schema,
            params=facts(args.facts),
            batch=args.batch,
        )
    except GateError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        print({"class": args.class_, "tier": args.tier, "content": output})
    else:
        if "/review/" in output:
            print(output)
            print("queued for review")
        else:
            print(output)


def facts(path):
    return (
        {} if not path else {"gold": __import__("json").loads(Path(path).read_text())}
    )


def token_command(args):
    ledger = Ledger()
    if args.action == "revoke":
        ledger.db.execute("UPDATE callers SET revoked=1 WHERE caller=?", (args.caller,))
        ledger.db.commit()
        return
    token = secrets.token_urlsafe(32)
    ledger.db.execute("UPDATE callers SET revoked=1 WHERE caller=?", (args.caller,))
    ledger.db.execute(
        "INSERT INTO callers(caller,token_hash,max_tier,created) VALUES(?,?,?,?)",
        (
            args.caller,
            hashlib.sha256(token.encode()).hexdigest(),
            args.max_tier,
            __import__("time").time(),
        ),
    )
    ledger.db.commit()
    print(token)
