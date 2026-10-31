from _emit import main, version
from scrapling.fetchers import Fetcher


def run(url):
    p = Fetcher.get(url, timeout=30)
    return dict(status=p.status, final_url=str(p.url), html=str(p.html_content), markdown=p.markdown(),
                headers=dict(p.headers), version=version("scrapling"), rung="get")


main(run)
