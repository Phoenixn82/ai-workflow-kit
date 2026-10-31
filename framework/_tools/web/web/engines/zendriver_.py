import asyncio
import zendriver as zd
from zendriver import cdp
from _emit import main, version
from _md import to_md


async def go(url):
    # This host's chromium reads ~/.config/chromium-flags.conf and forces --ozone-platform=wayland
    # regardless of env; override explicitly or it ignores the private X display.
    b = await zd.start(headless=False, browser_executable_path="/usr/bin/chromium",
                        browser_args=["--ozone-platform=x11", "--ozone-platform-hint=x11"])
    try:
        tab = b.main_tab
        docs, failures = [], []  # top-level document responses in arrival order; the last one is what we read

        def on_response(ev):
            # A page target's main frame id is its target id; iframes' documents carry their own frame ids.
            if ev.type_ == cdp.network.ResourceType.DOCUMENT and ev.frame_id == tab.target.target_id:
                docs.append(ev.response)

        def on_failed(ev):
            if ev.type_ == cdp.network.ResourceType.DOCUMENT:
                failures.append(ev.error_text)

        # Registered before navigating, so zendriver enables the Network domain first.
        tab.add_handler(cdp.network.ResponseReceived, on_response)
        tab.add_handler(cdp.network.LoadingFailed, on_failed)
        await tab.get(url)
        await tab.sleep(6)
        html = await tab.get_content()
        final_url = await tab.evaluate("location.href")
        final_url = final_url if isinstance(final_url, str) else None
        # zendriver's get() never raises for DNS/refused/TLS/timeout: Chrome just shows its net-error page.
        if (final_url or "").startswith("chrome-error:") or not docs:
            why = ", ".join(dict.fromkeys(failures)) or "no top-level document response"
            raise RuntimeError(f"navigation failed: {why} (final URL {final_url})")
        doc = docs[-1]
        return dict(status=doc.status, final_url=final_url or doc.url, headers=dict(doc.headers), html=html,
                    markdown=to_md(html), version=version("zendriver"), rung="browser")
    finally:
        await b.stop()


if __name__ == "__main__":
    main(lambda url: asyncio.run(go(url)))
