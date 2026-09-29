"""KCDW METARs (routine and special) from dynamical.org's ASOS GeoParquet, 2020 onward."""
import duckdb
from datetime import date
from pathlib import Path

BASE = 'https://data.source.coop/dynamical/asos-parquet'
OUT = Path('var/mos/metar.parquet')


def main(first_year=2020):
    con = duckdb.connect()
    con.execute('INSTALL httpfs; LOAD httpfs;')
    urls = ', '.join(f"'{BASE}/year={y}/data.parquet'" for y in range(first_year, date.today().year + 1))
    con.execute(f"""COPY (SELECT valid AT TIME ZONE 'UTC' AS valid_utc, drct, sknt, gust, tmpc, dwpc, vsby, mslp
                  FROM read_parquet([{urls}]) WHERE station = 'CDW' ORDER BY valid)
                  TO '{OUT}' (FORMAT parquet)""")
    n, first, last, gusts = con.execute(f"SELECT count(*), min(valid_utc), max(valid_utc), count(gust) FROM '{OUT}'").fetchone()
    print(f'{n} METARs {first} .. {last}; {gusts} with a reported gust')


if __name__ == '__main__':
    main()
