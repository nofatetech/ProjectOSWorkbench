#!/usr/bin/env python3
"""Check the WordPress.com publish engine (src/publish.py).

Two layers:
  1. PURE tests (always run, no network): title resolution, body prep, wikilink
     flattening, status normalization, payload build, dry-run, frontmatter
     writeback round-trip on a temp note.
  2. LIVE checks (opt-in, need creds in ~/.workbench/config.json):
       --token            mint an OAuth2 token (proves client_id/secret/login)
       --dry  <note.md>   build + render HTML, no network, no writeback
       --publish <note.md>  REAL publish/update (writes back wp_post_id + URL)

Run:  .venv/bin/python scripts/wp_publish_check.py            # pure tests only
      .venv/bin/python scripts/wp_publish_check.py --token
      .venv/bin/python scripts/wp_publish_check.py --dry path/to/note.md
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import publish  # noqa: E402
from config import load_config  # noqa: E402


def _pure_tests() -> int:
    fails = 0

    def check(name, cond):
        nonlocal fails
        print(f"  {'ok ' if cond else 'FAIL'} {name}")
        if not cond:
            fails += 1

    # title precedence
    check("title from frontmatter",
          publish._resolve_title({"title": "Front"}, "# H1\nbody", Path("f.md"))
          == ("Front", "frontmatter"))
    check("title from H1",
          publish._resolve_title({}, "# My Post\n\nbody", Path("f.md"))
          == ("My Post", "h1"))
    check("title from filename",
          publish._resolve_title({}, "just body", Path("my-note.md"))
          == ("my-note", "filename"))

    # body prep: strip leading H1 only when it's the title source; flatten wikilinks
    check("prep drops H1 when title came from H1",
          "# My Post" not in publish._prep_body("# My Post\n\nhi", "h1"))
    check("prep keeps H1 when title came from frontmatter",
          "# My Post" in publish._prep_body("# My Post\n\nhi", "frontmatter"))
    check("wikilink flatten plain", publish._prep_body("see [[Foo]]", "x") == "see Foo")
    check("wikilink flatten aliased",
          publish._prep_body("see [[Foo|Bar]]", "x") == "see Bar")

    # status normalization
    check("status draft default", publish._normalize_status(None) == "draft")
    check("status publish from 'public'", publish._normalize_status("public") == "publish")
    check("status draft from junk", publish._normalize_status("maybe") == "draft")

    # tags
    check("tags from list", publish._norm_tags(["a", "b"]) == ["a", "b"])
    check("tags from csv", publish._norm_tags("a, b ,c") == ["a", "b", "c"])
    check("tags empty", publish._norm_tags(None) == [])

    # markdown -> html
    html = publish._to_html("# H\n\n- one\n- two\n\n**bold**")
    check("md->html headings", "<h1>" in html)
    check("md->html lists", "<li>" in html and "two" in html)
    check("md->html bold", "<strong>bold</strong>" in html)

    # full build_payload + dry-run + writeback on a temp note
    with tempfile.TemporaryDirectory() as d:
        note = Path(d) / "hello-world.md"
        note.write_text(
            "---\ntype: post\ntags: [demo, test]\npublish: draft\n---\n\n"
            "# Hello World\n\nA paragraph with a [[wikilink]].\n",
            encoding="utf-8")

        payload, result, post_id = publish.build_payload(note)
        check("payload title", payload["title"] == "Hello World")
        check("payload status draft", payload["status"] == "draft")
        check("payload tags joined", payload.get("tags") == "demo,test")
        check("payload html no dup H1", "<h1>" not in payload["content"])
        check("payload html has wikilink text",
              "wikilink" in payload["content"] and "[[" not in payload["content"])
        check("no post_id yet", post_id == "")

        dry = publish.publish_note(publish.WPCreds(), note, dry_run=True)
        check("dry-run action", dry.action == "dry-run")
        check("dry-run carries html", dry.html_len > 0 and "<p>" in dry.html)
        check("dry-run wrote nothing", "wp_post_id" not in note.read_text())

        # --- category/tag policy ---
        opts = publish.PublishOptions(
            category="Civic-SENACYT", extra_tags=["My Project"],
            include_note_tags=True, tag_exclude=["test"])
        pl, res, _ = publish.build_payload(note, opts)
        check("policy: category from area", pl.get("categories") == "Civic-SENACYT")
        check("policy: project tag added", "My Project" in res.tags)
        check("policy: note tag kept", "demo" in res.tags)
        check("policy: excluded tag dropped ('test')", "test" not in res.tags)
        check("policy: result carries cats/tags", res.categories == ["Civic-SENACYT"])

        # exclude is case-insensitive + dedupe
        opts2 = publish.PublishOptions(extra_tags=["Demo"], tag_exclude=["DEMO"])
        _, res2, _ = publish.build_payload(note, opts2)
        check("policy: exclude case-insensitive", "Demo" not in res2.tags
              and "demo" not in [t.lower() for t in res2.tags])

        # --- visibility / password ---
        opts_priv = publish.PublishOptions(status="publish", visibility="private")
        plv, resv, _ = publish.build_payload(note, opts_priv)
        check("visibility private → status private", plv["status"] == "private")
        opts_pwd = publish.PublishOptions(status="publish", visibility="password",
                                          password="s3cret")
        plp, _, _ = publish.build_payload(note, opts_pwd)
        check("visibility password → password field", plp.get("password") == "s3cret")
        check("visibility password keeps status", plp["status"] == "publish")
        check("visibility synonym 'password-protected' → password",
              publish._normalize_visibility("password-protected") == "password")
        check("visibility synonym unknown → public",
              publish._normalize_visibility("whatever") == "public")

        # --- frontmatter overrides the policy ---
        note2 = Path(d) / "override.md"
        note2.write_text(
            "---\ntype: post\ntags: [a]\nwp_categories: [News]\n"
            "wp_tags: [only, these]\nvisibility: private\n---\n# T\nbody\n",
            encoding="utf-8")
        plo, reso, _ = publish.build_payload(note2, opts)
        check("fm wp_categories overrides", plo.get("categories") == "News")
        check("fm wp_tags overrides", reso.tags == ["only", "these"])
        check("fm visibility overrides (no opts vis)", plo["status"] == "private")

        # writeback round-trip (simulate a successful create)
        publish._writeback(note, "12345", "https://myweb1712.wordpress.com/?p=12345", "draft")
        txt = note.read_text()
        check("writeback wp_post_id", "wp_post_id: 12345" in txt)
        check("writeback url", "published_url: https://myweb1712.wordpress.com/?p=12345" in txt)
        check("writeback published_at", "published_at:" in txt)
        check("writeback preserved body", "A paragraph" in txt)
        check("writeback preserved existing key", "type: post" in txt)
        # a second writeback must REPLACE, not duplicate
        publish._writeback(note, "12345", "https://x/2", "publish")
        check("writeback idempotent (no dup wp_post_id)",
              note.read_text().count("wp_post_id:") == 1)
        check("writeback updated publish intent", "publish: publish" in note.read_text())

    # --- publish_note agent tool wiring (tools.py) ---
    import tools

    def names(schemas):
        return {s["function"]["name"] for s in schemas}
    off = tools.ToolContext(vault_root=Path("/tmp"))
    on = tools.ToolContext(vault_root=Path("/tmp"), publish_enabled=True,
                           publish_fn=lambda a: f"published {a.get('path')}")
    check("tool: hidden when publish disabled", "publish_note" not in names(tools.schemas_for(off)))
    check("tool: advertised when enabled", "publish_note" in names(tools.schemas_for(on)))
    check("tool: in MUTATING_TOOLS", "publish_note" in tools.MUTATING_TOOLS)
    check("tool: disabled guard message",
          "disabled" in tools.execute_tool("publish_note", {"path": "x.md"}, off).lower())
    check("tool: routes to publish_fn",
          tools.execute_tool("publish_note", {"path": "x.md"}, on) == "published x.md")

    print()
    if fails:
        print(f"PURE TESTS: {fails} FAILED")
    else:
        print("PURE TESTS: all passed")
    return fails


def _live(argv) -> int:
    cfg = load_config()
    creds = publish.creds_from_config(cfg)
    if "--token" in argv:
        print("\n[live] minting token…")
        try:
            tok = publish.mint_token(creds)
            print(f"  ok — token starts {tok[:8]}… (len {len(tok)})")
        except publish.PublishError as ex:
            print(f"  FAIL — {ex}")
            return 1
    for flag, dry in (("--dry", True), ("--publish", False)):
        if flag in argv:
            note = argv[argv.index(flag) + 1]
            print(f"\n[live] {'dry-run' if dry else 'PUBLISH'} {note}")
            try:
                opts = publish.PublishOptions(
                    default_status=getattr(cfg, "publish_default_status", "draft"))
                res = publish.publish_note(creds, note, dry_run=dry, options=opts)
                print(f"  action={res.action} status={res.status} "
                      f"title={res.title!r} url={res.url} id={res.post_id} "
                      f"cats={res.categories} tags={res.tags} "
                      f"html_len={res.html_len}")
                if dry:
                    print("  --- HTML preview (first 400) ---")
                    print("  " + res.html[:400].replace("\n", "\n  "))
            except publish.PublishError as ex:
                print(f"  FAIL — {ex}")
                return 1
    return 0


if __name__ == "__main__":
    rc = _pure_tests()
    if len(sys.argv) > 1:
        rc += _live(sys.argv[1:])
    sys.exit(1 if rc else 0)
