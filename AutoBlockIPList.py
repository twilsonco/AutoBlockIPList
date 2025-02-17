#!/usr/bin/env python3

import os
import shutil
from datetime import datetime
import validators
import argparse
import requests
import sqlite3
import ipaddress
import time
from functools import reduce


VERSION = "1.2.0"


def create_connection(db_file):
    try:
        return sqlite3.connect(db_file)
    except sqlite3.Error as e:
        raise e


def get_ip_remote(link):
    data = ""
    verbose(f"Fetching IP list from {link}")
    try:
        r = requests.get(link)
        r.raise_for_status()
        data = r.text.replace("\r", "")
    except requests.exceptions.RequestException as e:
        verbose(f"Unable to connect to {link}")
    return data


def get_ip_local(file):
    verbose(f"Reading IP list from {file.name}")
    return file.read().replace("\r", "")


def get_ip_list(local, external):
    data = [get_ip_local(f).split("\n") for f in local] + [get_ip_remote(s).split("\n") for s in external]
    ip = reduce(lambda a, b: a + b, data)
    return ip


def ipv4_to_ipstd(ipv4):
    numbers = [int(bits) for bits in ipv4.split('.')]
    return '0000:0000:0000:0000:0000:ffff:{:02x}{:02x}:{:02x}{:02x}'.format(*numbers).upper()


def ipv6_to_ipstd(ipv6):
    return ipaddress.ip_address(ipv6).exploded.upper()


def process_ip(ip_list, expire, min_cidr_prefix=12):
    processed_ips = set()
    cidr_networks = set()
    invalid = set()
    total_potential_ips = 0
    cidr_ip_count = 0
    skipped_cidr_ip_count = 0
    skipped_cidr_network_count = 0

    for line in ip_list:
        for i in line.strip().split():
            if not i or i.startswith("#") or "." not in i:
                continue  # Skip empty lines and comments

            try:
                if '/' in i:
                    network = ipaddress.ip_network(i)
                    total_potential_ips += network.num_addresses
                    if network.prefixlen >= min_cidr_prefix:
                        cidr_networks.add(i)
                        cidr_ip_count += network.num_addresses
                    else:
                        skipped_cidr_network_count += 1
                        skipped_cidr_ip_count += network.num_addresses
                else:
                    # Process as single IP
                    ip = ipaddress.ip_address(i)
                    if ip.version == 4:
                        ipstd = ipv4_to_ipstd(i)
                    elif ip.version == 6:
                        ipstd = ipv6_to_ipstd(i)
                    else:
                        ipstd = ""
                    processed_ips.add((i, ipstd, expire))
                    total_potential_ips += 1

            except ValueError:
                if i != "":
                    invalid.add(i)
    
    # Sort the list of CIDR networks in descending order of trailing prefix (increasing order of network size) and then by IP address
    cidr_networks = sorted(cidr_networks, key=lambda x: (-int(x.split('/')[1]), x.split('/')[0]))
    
    if skipped_cidr_network_count > 0:
        verbose(f"Skipped {skipped_cidr_network_count} CIDR networks with {skipped_cidr_ip_count} IPs due to being too large to process")

    return list(processed_ips), list(cidr_networks), list(invalid), cidr_ip_count

def expand_cidr(cidr_str, expire):
    expanded_ips = []
    try:
        network = ipaddress.ip_network(cidr_str)
        for ip in network:
            ip_str = str(ip)
            if ip.version == 4:
                ipstd = ipv4_to_ipstd(ip_str)
            else:
                ipstd = ipv6_to_ipstd(ip_str)
            expanded_ips.append((ip_str, ipstd, expire))

    except ValueError as e:
        verbose(f"Error expanding CIDR {cidr_str}: {e}")

    return expanded_ips


def url(link):
    if validators.url(link) != True:
        raise argparse.ArgumentError
    return link


def folder(attr='r'):
    def check_folder(path):
        if os.path.isdir(path):
            if attr == 'r' and not os.access(path, os.R_OK):
                raise argparse.ArgumentTypeError(f'"{path}" is not readable.')
            if attr == 'w' and not os.access(path, os.W_OK):
                raise argparse.ArgumentTypeError(f'"{path}" is not writable.')
            return os.path.abspath(path)
        else:
            raise argparse.ArgumentTypeError(f'"{path}" is not a valid path.')
    return check_folder


def verbose(message):
    global args
    if args.verbose:
        print(message)


def parse_args():
    parser = argparse.ArgumentParser(prog='AutoBlockIPList', description='''Add IP addresses to Synology AutoBlock database from one or more lists from files or URLs.

Example IP lists:

Simple list:
83.222.191.62
218.92.0.111
218.92.0.114

Lists with multiple columns:
83.222.191.62 8
218.92.0.111 8
218.92.0.114 8

Lists with CIDR notation:
1.10.16.0/20
1.19.0.0/16
1.32.128.0/18
''')
    parser.add_argument("-f","--in-file", nargs='*', type=argparse.FileType('r'), default=[],
                        help="Local list file separated by a space "
                        "(eg. /home/user/list.txt custom.txt)")
    parser.add_argument("-u", "--in-url", nargs="*", type=url, default=[],
                        help="External list url separated by a space "
                        "(eg https://example.com/list.txt https://example.com/all.txt)")
    parser.add_argument("-e", "--expire-in-day", type=int, default=0,
                        help="Expire time in day. Default 0: no expiration")
    parser.add_argument("--remove-expired", action='store_true',
                        help="Remove expired entry")
    parser.add_argument("-b", "--backup-to", type=folder('w'),
                        help="Folder to store a backup of the database")
    parser.add_argument("--clear-db", action='store_true',
                        help="Clear ALL deny entry in database before filling")
    parser.add_argument("--dry-run", action='store_true',
                        help="Perform a run without any modifications")
    parser.add_argument("--batch-size", type=int, default=100000,
						help="Limit number of database rows modified per batch for removing expired extries or clearing thedatabase. Default 100,000")
    parser.add_argument("--disable-journaling", action='store_true',
                        help="Disable journaling when modifying database. WARNING: this can result in database error in case of a crash, and should only be used if a small batch size doesn't alleviate errors related to database or disk being full!")
    parser.add_argument("-v", "--verbose", action='store_true',
                        help="Increase output verbosity")
    parser.add_argument("--db-location", type=str, default="/etc/synoautoblock.db", help="Location of the Synology AutoBlock database")
    parser.add_argument("--min-cidr-prefix", type=int, default=24, help="Minimum CIDR prefix to process. Smaller values allow larger networks. Default 24: only add networks with 256 addresses or less")
    parser.add_argument('--version', action='version', version=f'%(prog)s version {VERSION}')

    a = parser.parse_args()

    if len(a.in_file) == 0 and len(a.in_url) == 0:
        raise parser.error("At least one source list is mandatory (file or url)")
    if a.clear_db and a.backup_to is None:
        raise parser.error("backup folder should be set for clear db")
    if a.dry_run:
        a.verbose = True

    return a


if __name__ == '__main__':
    start_time = time.time()
    args = parse_args()

    # define the path of the database
    # DSM 6: "/etc/synoautoblock.db"
    # DSM 7: should be the same (TODO confirm path)
    db = args.db_location

    db_present = os.path.isfile(db)
    db_accessible = db_present and os.access(db, os.R_OK)
    
    if not args.dry_run and not db_present:
        raise FileNotFoundError(f"No such file or directory: '{db}'")
    if not args.dry_run and not db_accessible:
        raise FileExistsError("Unable to read database. Run this script with sudo or root user.")

    if args.backup_to is not None and db_present:
        filename = datetime.now().strftime("%Y%m%d_%H%M%S") + "_backup_synoautoblock.db"
        shutil.copy(db, os.path.join(args.backup_to, filename))
        verbose("Database successfully backup")

    if args.expire_in_day > 0:
        args.expire_in_day = int(start_time) + args.expire_in_day * 60 * 60 * 24

    ips = get_ip_list(args.in_file, args.in_url)
    ips_formatted, cidr_networks, ips_invalid, cidr_ip_count = process_ip(ips, args.expire_in_day, args.min_cidr_prefix)
    simple_ip_count = len(ips_formatted)
    total_count = simple_ip_count + cidr_ip_count
    verbose(f"IPs parsed from lists to be added: {simple_ip_count}")
    verbose(f"Total CIDR networks: {len(cidr_networks)}")
    verbose(f"Total IP in CIDR networks: {cidr_ip_count}")
    verbose(f"Total IP invalid: {len(ips_invalid)}")
    verbose(f"Total potential IP: {total_count}")

    if simple_ip_count + len(cidr_networks) > 0:
        if db_accessible:
            conn = create_connection(db)
            c = conn.cursor()

        if not args.dry_run and db_accessible:
            if args.disable_journaling:
            	verbose("WARNING!!! Database journaling is disabled. DO NOT quit the program before it finishes!")
            	c.execute("PRAGMA journal_mode = OFF")
            else:
                c.execute("PRAGMA journal_mode = MEMORY")
            conn.commit()

        if args.remove_expired and not args.dry_run and db_accessible:
            while True:
                c.execute("""
    DELETE FROM AutoBlockIP 
    WHERE rowid IN (
        SELECT rowid FROM AutoBlockIP 
        WHERE Deny = 1 
        AND ExpireTime > 0 
        AND ExpireTime < strftime('%s','now')
        LIMIT ?
    )
""", (args.batch_size,))
                if c.rowcount == 0:
                    break
                conn.commit()
            verbose("All expired entry was successfully removed")

        if args.clear_db and not args.dry_run and db_accessible:
            while True:
                c.execute("""
    DELETE FROM AutoBlockIP 
    WHERE Deny = 1 
    AND rowid IN (
        SELECT rowid FROM AutoBlockIP 
        LIMIT ?
    )
""", (args.batch_size,))
                if c.rowcount == 0:
                    break
                conn.commit()
            verbose("All deny entry was successfully removed")
        
        if not args.dry_run and db_accessible:
            conn.commit()
            c.execute("VACUUM")
            conn.commit()

        if db_accessible:
            nb_ip = c.execute("SELECT COUNT(IP) FROM AutoBlockIP WHERE Deny = 1")
            nb_ip_before = nb_ip.fetchone()[0]
            current_count = int(nb_ip_before)
            verbose(f"Total deny IP currently in your Synology DB: {nb_ip_before}")
            
        if simple_ip_count > 0:
            if not args.dry_run and db_accessible:
                verbose(f"Adding {simple_ip_count} IPs to the database from simple IP lists")
                try:
                    c.executemany("REPLACE INTO AutoBlockIP (IP, IPStd, ExpireTime, Deny, RecordTime, Type, Meta) "
                        "VALUES(?, ?, ?, 1, strftime('%s','now'), 0, NULL);", ips_formatted)
                    conn.commit()
                    current_count += simple_ip_count
                except sqlite3.Error as e:
                    raise e

        if len(cidr_networks) > 0:
            verbose(f"Adding {cidr_ip_count} IPs to the database from {len(cidr_networks)} CIDR networks")
            for i, cidr_str in enumerate(cidr_networks):
                ips_formatted = expand_cidr(cidr_str, args.expire_in_day)
                if len(ips_formatted) > 0 and not args.dry_run and db_accessible:
                    try:
                        c.executemany("REPLACE INTO AutoBlockIP (IP, IPStd, ExpireTime, Deny, RecordTime, Type, Meta) "
                            "VALUES(?, ?, ?, 1, strftime('%s','now'), 0, NULL);", ips_formatted)
                        conn.commit()
                        current_count += len(ips_formatted)
                    except sqlite3.Error as e:
                        print(f"Error adding CIDR {cidr_str} to the database: {e}")
                        remaining_cidr_network_count = len(cidr_networks) - i
                        remaining_IP_count = total_count - current_count
                        print(f"Remaining {remaining_cidr_network_count} CIDR networks and {remaining_IP_count} IPs will not be added to the database.")
                        conn.rollback()
                        break
                
        if db_accessible:
            nb_ip = c.execute("SELECT COUNT(IP) FROM AutoBlockIP WHERE Deny = 1")
            nb_ip_after = nb_ip.fetchone()[0]
            conn.close()
            verbose(f"Total deny IP now in your Synology DB: {nb_ip_after} ({nb_ip_after - nb_ip_before} added)")
        
        if not db_accessible:
            verbose("No database access. No changes were made to the database")
        if args.dry_run:
            verbose("Dry run mode. No changes were made to the database")
    else:
        verbose("No IP found in list")

    elapsed = round(time.time() - start_time, 2)
    verbose(f"Elapsed time: {elapsed} seconds")
