"""Restore an authenticated snapshot into a new empty directory, never a live app."""
import argparse
from pathlib import Path
from dotenv import load_dotenv
from backup_service import restore_backup

def main():
    load_dotenv(Path(__file__).resolve().parent/'.env')
    parser=argparse.ArgumentParser()
    parser.add_argument('snapshot');parser.add_argument('empty_destination')
    arguments=parser.parse_args()
    destination=restore_backup(arguments.snapshot,arguments.empty_destination)
    print('Backup verified and restored to '+str(destination))

if __name__=='__main__':main()
