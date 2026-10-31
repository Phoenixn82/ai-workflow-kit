from _emit import main, version
from scrapling.fetchers import DynamicFetcher


def run(url):
    p = DynamicFetcher.fetch(url, headless=False, network_idle=True, timeout=60000)
    return dict(status=p.status, final_url=str(p.url), html=str(p.html_content), markdown=p.markdown(),
                headers=dict(p.headers), version=version("scrapling"), rung="fetch")


main(run)
