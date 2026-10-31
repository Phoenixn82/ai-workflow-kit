from _emit import main, version
from scrapling.fetchers import StealthyFetcher


def run(url):
    # solve_cloudflare clicks the Turnstile checkbox; passive checks only (spec §2)
    p = StealthyFetcher.fetch(url, headless=False, solve_cloudflare=False, network_idle=True, timeout=90000)
    return dict(status=p.status, final_url=str(p.url), html=str(p.html_content), markdown=p.markdown(),
                headers=dict(p.headers), version=version("scrapling"), rung="stealth")


main(run)
