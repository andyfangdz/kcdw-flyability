"""KCDW ASOS 1-minute observations (IEM), month by month, resumable.

Each minute carries the 2-minute average wind (sknt, drct) and the peak 5-second
gust in that minute (gust_sknt, gust_drct), so the hourly peak gust is measured
rather than limited by METAR gust-reporting criteria. METAR targets come from
dynamical.org's ASOS parquet (fetch_metar.py).
"""
import csv, io, time, urllib.request
from datetime import date
from pathlib import Path

OUT = Path('var/mos/obs')
URL = ('https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py?station={station}&tz=UTC'
       '&year1={y1}&month1={m1}&day1=1&hour1=0&minute1=0&year2={y2}&month2={m2}&day2=1&hour2=0&minute2=0'
       '&vars=sknt&vars=drct&vars=gust_sknt&vars=gust_drct&sample=1min&what=download&delim=comma')


def months(start, end):
    y, m = start
    while (y, m) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def main(first=(2020, 10), station='CDW', out=OUT):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    today = date.today()
    for y, m in months(first, (today.year, today.month)):
        path = out / f'{y}-{m:02d}.csv'
        if path.exists() and (y, m) != (today.year, today.month):
            continue
        y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
        for attempt in range(4):
            try:
                text = urllib.request.urlopen(URL.format(station=station, y1=y, m1=m, y2=y2, m2=m2), timeout=300).read().decode()
                break
            except Exception:
                time.sleep(10 * (attempt + 1))
        else:
            print(f'{y}-{m:02d} failed', flush=True)
            continue
        rows = list(csv.reader(io.StringIO(text)))
        if not rows or rows[0][:3] != ['station', 'station_name', 'valid(UTC)']:
            print(f'{y}-{m:02d} unexpected response', flush=True)
            continue
        path.write_text(text)
        print(f'{y}-{m:02d} {len(rows) - 1} minutes', flush=True)
        time.sleep(2)


if __name__ == '__main__':
    import sys
    # Usage: fetch_obs.py [STATION OUT_DIR]; defaults to KCDW into var/mos/obs.
    if len(sys.argv) == 3:
        main(station=sys.argv[1], out=sys.argv[2])
    else:
        main()
