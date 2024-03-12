import inspect
import json
import logging
from logging.config import fileConfig
import os
import time
import datetime
import cbor2
import ftplib
import psycopg2
import psycopg2.extras
from psycopg2 import pool
import configparser

from utils import CRC, normalize_timestamp

config = configparser.ConfigParser()
config.read(['ftp.ini', 'database.ini'])

# FTP Configuration
FTP_HOST = config['ftp']['host']
FTP_USER = config['ftp']['user']
FTP_PASS = config['ftp']['password']



# configure logging
fileConfig('logging.ini')
logger = logging.getLogger('ftpLogger')

# Load and parse the FTP and Database configurations


# Database configuration
DEV_DATABASE_CONFIG = {
    "user": config['dev']['user'],
    "password": config['dev']['password'],
    "database": config['dev']['database'],
    "host": config['dev']['host'],
    "port": config['dev']['port'],
}

PROD_DATABASE_CONFIG = {
    "user": config['prod']['user'],
    "password": config['prod']['password'],
    "database": config['prod']['database'],
    "host": config['prod']['host'],
    "port": config['prod']['port'],
}

# Connect to the database
DevDB_connection = pool.SimpleConnectionPool(
    1,
    10,
    dbname=DEV_DATABASE_CONFIG["database"],
    user=DEV_DATABASE_CONFIG["user"],
    password=DEV_DATABASE_CONFIG["password"],
    host=DEV_DATABASE_CONFIG["host"],
    port=DEV_DATABASE_CONFIG["port"],
)

ProdDB_connection = pool.SimpleConnectionPool(
    1,
    10,
    dbname=PROD_DATABASE_CONFIG["database"],
    user=PROD_DATABASE_CONFIG["user"],
    password=PROD_DATABASE_CONFIG["password"],
    host=PROD_DATABASE_CONFIG["host"],
    port=PROD_DATABASE_CONFIG["port"],
)

batch = []
batch_size = 50


# Connect to the FTP server
def connect_to_ftp():
    try:
        ftp = ftplib.FTP(FTP_HOST, FTP_USER, FTP_PASS)
        ftp.cwd("/")
        print("Successfully connected to FTP server.")
        return ftp
    except Exception as e:
        print(f"Failed to connect to FTP server: {e}")
        return None


def check_and_process_files(ftp):
    current_files = ftp.nlst()  # List of files in the current directory
    folder_names = [
        "cfg",
        "jake",
        "kyle",
        "test",
        "write",
        "archive",
    ]  # List of folder names to ignore

    for filename in current_files:
        if filename in folder_names or os.path.splitext(filename)[1]:
            continue  # Skip folders and files with extensions

        process_file(ftp, filename)
        # Define the source and destination paths
        source_path = filename
        destination_path = f"archive/{filename}"

        # Move the file to the 'archive' folder
        try:
            ftp.rename(source_path, destination_path)
            logger.info(f"Moved {filename} to archive folder successfully.")
        except Exception as e:
            logger.info(f"Error moving {filename} to archive folder: {e}")

# Process the file
def process_file(ftp, filename):
    global batch
    local_filename = os.path.join("/tmp", filename)
    with open(local_filename, "wb") as local_file:
        ftp.retrbinary("RETR " + filename, local_file.write)

    # Read the file content
    with open(local_filename, "rb") as local_file:
        file_content = local_file.read()

    device_id = filename.split("_")[0]

    json_strings = file_content.split(b"xxx")
    for json_str in json_strings:
        if CRC(json_str, False):
            if len(json_str) >= 20:
                data = cbor2.loads(json_str)
                data = dict(data)
                data["id"] = device_id
                batch.append(data)
                if len(batch) >= 50:
                    # Insert the batch into the database
                    insert_batch(batch, DevDB_connection, ProdDB_connection)
    if len(batch) > 0:
        insert_batch(batch, DevDB_connection, ProdDB_connection)
        batch = []
    os.remove(local_filename)
def get_caller_filename():
    return inspect.stack()[2].filename
def insert_batch(batch, DevDB_connection, ProdDB_connection):
    """Inserts or updates a batch of records into the PostgreSQL database based on a conflict."""
    try:
        with DevDB_connection.getconn() as dev_conn, ProdDB_connection.getconn() as prod_conn:
            DevCursor = dev_conn.cursor()
            ProdCursor = prod_conn.cursor()

            transformed_batch = []
            for data in batch:
                if "ts" in data and "id" in data:
                    try:
                        created_timestamp = datetime.datetime.now().replace(microsecond=0)
                        adjusted_timestamp = normalize_timestamp(data["ts"])
                        date_format = '%Y-%m-%d %H:%M:%S'
                        adjusted_timestamp = datetime.datetime.strptime(adjusted_timestamp[:-3], date_format)
                    except Exception as e:
                        logger.error(
                            f"Error in normalize_timestamp (from {get_caller_filename()}): {e}"
                        )
                        continue

                    record = (
                        created_timestamp,
                        adjusted_timestamp,
                        json.dumps(data),
                        data["id"],
                    )
                    transformed_batch.append(record)
                else:
                    logger.warning("Missing 'ts' in data, skipping record.")

            if transformed_batch:
                query = """
                INSERT INTO mqtttest (created, time, data, device_id)
                VALUES %s
                """

                psycopg2.extras.execute_values(
                    DevCursor, query, transformed_batch, template=None
                )

                psycopg2.extras.execute_values(
                    ProdCursor, query, transformed_batch, template=None
                )

                dev_conn.commit()
                prod_conn.commit()
            else:
                logger.warning("No valid data to insert.")

    except Exception as e:
        logger.error(f"Error inserting batch into database: {e}")

# Main loop
def main():
    while True:
        try:
            ftp = connect_to_ftp()
            check_and_process_files(ftp)
            ftp.quit()
            time.sleep(20)
        except Exception as e:
            logger.error(f"Error: {e}")


if __name__ == "__main__":
    main()
