import csv
import gzip
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
BUILD.mkdir(exist_ok=True)

ANNCSU_URL = (
    "https://anncsu.open.agenziaentrate.gov.it/"
    "age-inspire/opendata/anncsu/getds.php?INDIR_ITA="
)

OSM_PAGE = "https://openstreetdata.org/"
OSM_FALLBACK = "https://openstreetdata.org/data/IT-houses.tsv.gz"

HEADERS = {
    "User-Agent": "Italy-All-House-Map/1.0 (open-data project)"
}

ITALY_BOUNDS = {
    "min_lon": 5.5,
    "max_lon": 20.0,
    "min_lat": 34.0,
    "max_lat": 48.5,
}


def download(url, destination):
    print("Downloading:", url)

    with requests.get(
        url,
        headers=HEADERS,
        stream=True,
        timeout=(30, 300)
    ) as response:

        response.raise_for_status()

        with open(destination, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)

    if not os.path.exists(destination) or os.path.getsize(destination) == 0:
        raise RuntimeError("Downloaded file is empty: " + str(destination))

    print("Downloaded:", os.path.getsize(destination), "bytes")


def number(value):
    if value is None:
        return None

    value = str(value).strip()

    if not value:
        return None

    value = value.replace(",", ".")

    try:
        return float(value)
    except ValueError:
        return None


def valid_italy_coordinate(lon, lat):
    if lon is None or lat is None:
        return False

    return (
        ITALY_BOUNDS["min_lon"] <= lon <= ITALY_BOUNDS["max_lon"]
        and ITALY_BOUNDS["min_lat"] <= lat <= ITALY_BOUNDS["max_lat"]
    )


def clean(value):
    if value is None:
        return ""

    return str(value).strip()


def normalize_header(value):
    return clean(value).replace("\ufeff", "").strip().lower()


def write_feature(file, lon, lat, properties):
    feature = {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [lon, lat]
        },
        "properties": properties
    }

    file.write(
        json.dumps(feature, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )


def read_csv_reader(text):
    sample = text.read(65536)
    text.seek(0)

    try:
        delimiter = csv.Sniffer().sniff(
            sample,
            delimiters=";,\t|"
        ).delimiter
    except csv.Error:
        delimiter = ";"

    return csv.DictReader(text, delimiter=delimiter)


def find_anncsu_csvs(zip_path):
    archive = zipfile.ZipFile(zip_path)

    matches = []

    for name in archive.namelist():
        if not name.lower().endswith(".csv"):
            continue

        try:
            with archive.open(name) as raw:
                text = io.TextIOWrapper(
                    raw,
                    encoding="utf-8-sig",
                    errors="replace",
                    newline=""
                )

                reader = read_csv_reader(text)

                headers = {
                    normalize_header(h)
                    for h in (reader.fieldnames or [])
                }

                if (
                    "civico" in headers
                    and "odonimo" in headers
                    and "coord_x_comune" in headers
                    and "coord_y_comune" in headers
                ):
                    matches.append(name)

        except Exception as error:
            print("Skipping CSV:", name, error)

    return archive, matches


def anncsu_number(row):
    civic = clean(row.get("CIVICO"))
    exponent = clean(row.get("ESPONENTE"))
    specificity = clean(row.get("SPECIFICITA"))
    metric = clean(row.get("METRICO"))
    snc = clean(row.get("PROGRESSIVO_SNC"))

    if not civic:
        civic = metric

    if not civic and snc:
        civic = "SNC"

    result = civic

    if exponent:
        result += " " + exponent

    if specificity:
        result += " " + specificity

    return result.strip()


def method_description(value):
    descriptions = {
        "1": "Field survey, estimated accuracy under 5 m",
        "2": "Field survey, estimated accuracy 5 m or more",
        "3": "Indirect source, estimated accuracy under 5 m",
        "4": "Indirect source, estimated accuracy 5 m or more",
        "5": "Municipal portal indirect positioning"
    }

    return descriptions.get(
        clean(value),
        "Method not specified"
    )


def process_anncsu_csv(text, output):
    reader = read_csv_reader(text)

    count = 0
    skipped = 0

    for row in reader:
        row = {
            normalize_header(k).upper(): clean(v)
            for k, v in row.items()
            if k is not None
        }

        lon = number(row.get("COORD_X_COMUNE"))
        lat = number(row.get("COORD_Y_COMUNE"))

        if not valid_italy_coordinate(lon, lat):
            skipped += 1
            continue

        civic = anncsu_number(row)

        if not civic:
            skipped += 1
            continue

        street = row.get("ODONIMO", "")

        city = (
            row.get("NOME_COMUNE")
            or row.get("COMUNE")
            or row.get("CODICE_ISTAT", "")
        )

        properties = {
            "source": "ANNCSU",
            "id": row.get("PROGRESSIVO_ACCESSO", ""),
            "street": street,
            "number": civic,
            "city": city,
            "postcode": "",
            "method": method_description(row.get("METODO"))
        }

        write_feature(output, lon, lat, properties)
        count += 1

    return count, skipped


def process_anncsu(zip_path, output_path):
    print("\nProcessing official ANNCSU data...")

    count = 0
    skipped = 0

    with open(output_path, "w", encoding="utf-8") as output:

        if zipfile.is_zipfile(zip_path):
            archive, members = find_anncsu_csvs(zip_path)

            if not members:
                raise RuntimeError(
                    "No ANNCSU address CSV with the expected coordinate "
                    "columns was found in the national ZIP."
                )

            print("ANNCSU CSV files:", members)

            for name in members:
                with archive.open(name) as raw:
                    text = io.TextIOWrapper(
                        raw,
                        encoding="utf-8-sig",
                        errors="replace",
                        newline=""
                    )

                    added, ignored = process_anncsu_csv(text, output)
                    count += added
                    skipped += ignored

            archive.close()

        else:
            with open(
                zip_path,
                "r",
                encoding="utf-8-sig",
                errors="replace",
                newline=""
            ) as text:

                count, skipped = process_anncsu_csv(text, output)

    print("ANNCSU points:", count)
    print("ANNCSU records skipped:", skipped)

    if count == 0:
        raise RuntimeError(
            "No georeferenced ANNCSU civic numbers were produced."
        )

    return count


def find_osm_download_url():
    print("\nFinding OpenStreetData Italy house download...")

    try:
        response = requests.get(
            OSM_PAGE,
            headers=HEADERS,
            timeout=60
        )
        response.raise_for_status()

        links = re.findall(
            r'''href=["']([^"']*IT-houses\.tsv\.gz[^"']*)["']''',
            response.text,
            flags=re.IGNORECASE
        )

        if links:
            return urljoin(OSM_PAGE, links[0])

    except Exception as error:
        print("Could not inspect OpenStreetData page:", error)

    return OSM_FALLBACK


def get_field(row, *names):
    normalized = {
        normalize_header(k): clean(v)
        for k, v in row.items()
        if k is not None
    }

    for name in names:
        value = normalized.get(normalize_header(name))

        if value:
            return value

    return ""


def process_osm(osm_path, output_path):
    print("\nProcessing supplementary OpenStreetMap addresses...")

    count = 0
    skipped = 0

    with gzip.open(
        osm_path,
        "rt",
        encoding="utf-8-sig",
        errors="replace",
        newline=""
    ) as text, open(output_path, "w", encoding="utf-8") as output:

        reader = csv.DictReader(text, delimiter="\t")

        if not reader.fieldnames:
            raise RuntimeError("OpenStreetData TSV has no header.")

        for row in reader:
            lat = number(get_field(
                row, "lat", "latitude", "y"
            ))

            lon = number(get_field(
                row, "lon", "lng", "longitude", "x"
            ))

            civic = get_field(
                row,
                "housenumber",
                "house_number",
                "addr:housenumber",
                "number"
            )

            if not valid_italy_coordinate(lon, lat) or not civic:
                skipped += 1
                continue

            street = get_field(
                row, "street", "addr:street"
            )

            city = get_field(
                row,
                "city",
                "town",
                "municipality",
                "place"
            )

            postcode = get_field(
                row, "postcode", "postal_code", "zip"
            )

            properties = {
                "source": "OSM",
                "id": get_field(row, "id", "osm_id"),
                "street": street,
                "number": civic,
                "city": city,
                "postcode": postcode,
                "method": "OpenStreetMap community data"
            }

            write_feature(output, lon, lat, properties)
            count += 1

    print("OSM points:", count)
    print("OSM records skipped:", skipped)

    return count


def main():
    anncsu_zip = BUILD / "anncsu-national.zip"
    osm_file = BUILD / "IT-houses.tsv.gz"

    anncsu_output = BUILD / "anncsu.geojsonl"
    osm_output = BUILD / "osm.geojsonl"

    download(ANNCSU_URL, anncsu_zip)

    anncsu_count = process_anncsu(
        anncsu_zip,
        anncsu_output
    )

    osm_count = 0

    try:
        osm_url = find_osm_download_url()
        download(osm_url, osm_file)

        osm_count = process_osm(
            osm_file,
            osm_output
        )

    except Exception as error:
        print(
            "WARNING: Supplementary OSM data could not be loaded:",
            error
        )
        print("The map will still be built with official ANNCSU data.")
        osm_output.write_text("", encoding="utf-8")

    stats = {
        "anncsu_points": anncsu_count,
        "osm_points": osm_count,
        "total_points": anncsu_count + osm_count,
        "coordinate_bounds": ITALY_BOUNDS
    }

    (BUILD / "stats.json").write_text(
        json.dumps(stats, indent=2),
        encoding="utf-8"
    )

    print("\nBUILD SUMMARY")
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
