#!/usr/bin/env python3
"""Builds a test CMDB from a real-format equipment inventory (serial + status).

The source format is the two-column export IT keeps for store-room stock:

    EquipmentSerialNo.    STATUS
    5CG01523C7            Retired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 840
    PC0MLJ58              lenovo(22/10/23)level 5 store

That has only two of the nine attributes the CMDB needs, so this script
derives the rest:

  * deviceModel   - from vendor hints in the status text, else inferred from
                    the serial-number format (HP/Lenovo/Acer/Dell/Apple all
                    use recognisable patterns). Records where the model was
                    inferred rather than read carry modelInferred=True.
  * status        - free-text location strings mapped onto a small enum.
  * assignedUser,
    deviceAgeMonths,
    warrantyActive,
    repairCount,
    employeeRole  - synthesised deterministically from a hash of the serial,
                    so re-running produces byte-identical records and tests
                    stay reproducible.

A handful of serials are then overridden by RULE_COVERAGE below so that every
branch of agents/lifecycle_decision.py is reachable from this data set.

Usage:
    python scripts/seed_test_assets.py                 # dry run + report
    python scripts/seed_test_assets.py --apply         # write to DynamoDB
    python scripts/seed_test_assets.py --csv out.csv   # dump derived records
"""
import argparse
import csv
import hashlib
import os
import re
import sys
from collections import Counter

REGION = os.environ.get("CMDB_REGION", "ap-southeast-1")
TABLE_NAME = os.environ.get("CMDB_TABLE_NAME", "assetguardian-cmdb")

# The API rejects anything outside this set on asset_id / serial_number —
# see lambda/harness_invoke/agents/sanitize.py:sanitize_asset_id.
SERIAL_ALLOWED = re.compile(r"[A-Za-z0-9\-_]+")

# ---------------------------------------------------------------------------
# Source inventory, verbatim. Two tab/space-separated columns; blank status is
# allowed. Edit this block to test against a different stock list.
# ---------------------------------------------------------------------------
RAW_INVENTORY = """
5CG01523C7\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 840
5CG01522YY\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 843
5CG01522YT\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 847
5CG016D673\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 848
5CG01523C9\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 855
5CG01523BV\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 856
5CG01522Y2\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 857
5CG01522Y8\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 858
5CG01522YF\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 859
5CG01522XX\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 860
5CG01523C2\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 861
5CG01523CC\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 862
5CG01522XQ\tRetired IN STORE(16/9/2025) in cupboard 31(EMIS)HP ELITEBOOK 864
5CG0216VSQ\tLEVEL 16 STORE ROOM CUBBOARD
5CG0216W58\tLEVEL 16 STORE ROOM CUBBOARD
5CG0216W6H\tLEVEL 16 STORE ROOM CUBBOARD
5CG0216W67\tLEVEL 16 STORE ROOM CUBBOARD
5CG0216W8R\tLEVEL 16 STORE ROOM CUBBOARD
5CG043C9C8\tLEVEL 16 STORE ROOM CUBBOARD
5CG02746MT\tLEVEL 16 STORE ROOM CUBBOARD
5CG0296Q1P\tLEVEL 16 STORE ROOM CUBBOARD
5CG0107JLT\tRETURNED TO STORE(23/9/2025 (LF))
PC0MLG7U\tLEVEL 16 STORE ROOM CUBBOARD(30/9/2025)
R00WXJSK\tLEVEL 16 STORE ROOM CUBBOARD(30/9/2025)
NXGGMSG001704053A57600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXGVZSG00D024F7917600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXGVZSG00D0240F78E7600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXGVZSG00D0240F7D37600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXVPNSG00212423E407600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXGVZSG00D0240F7C87600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXVPNSG00212423E1F7600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXVPKSG00T2061F3047600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXVPNSG00212423E497600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXVPKSG00T2061F33D7600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXVPKSG00T2061F31E7600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXVPKSG00T2061F3357600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
NXVPNSG00212423E397600\tLEVEL 16 STORE ROOM CUBBOARD(3/10/2025)
2H1QN3\tLEVEL 5 STORE ROOM(7TH OCT 2025)
JDS6NW2(3400)\tLEVEL 5 STORE ROOM(7TH OCT 2025)
12337M3(DELL PRO SUPPORT FLEX)\tLEVEL 5 STORE ROOM(7TH OCT 2025)
FPK55X3(DELL FLEX)\tLEVEL 5 STORE ROOM(7TH OCT 2025)
B2ZQ5M3(DELL FLEX)\tLEVEL 5 STORE ROOM(7TH OCT 2025)
IPY1VQ3(DELL FLEX)\tLEVEL 5 STORE ROOM(7TH OCT 2025)
5CD016D681\tLEVEL 5 STORE ROOM(7TH OCT 2025)
5CG320B18\tLEVEL 5 STORE ROOM(7TH OCT 2025)
5CD0128PHV\tlevel 16 store room (8/10/2025)
5CD0128PHN\tlevel 16 store room (8/10/2025)
5CD01522YP\tlevel 16 store room (8/10/2025)
PC0MLJ45\tLEVEL 16 STORE R00M(14/10/2025)
PC0MLJ58\tlenovo(22/10/23)level 5 store
PC0HKYC1\tlenovo(22/10/23)level 5 store
PC0HKYAW\tlenovo(22/10/23)level 5 store
PC0MLF87\tlenovo(22/10/23)level 5 store
PC0MLHJW\tlenovo(22/10/23)level 5 store
PC0MLJ8M\tlenovo(22/10/23)level 5 store
PC0MLJ43\tlenovo(22/10/23)level 5 store
PC0MLG8M\tlenovo(22/10/23)level 5 store
PC0MLHK2\tlenovo(22/10/23)level 5 store
PC0MLFBU\tlenovo(22/10/23)level 5 store
PC0MLHJZ\tlenovo(22/10/23)level 5 store
PC0MLFBK\tlenovo(22/10/23)level 5 store
PC0JXAF6\tlenovo(22/10/23)level 5 store
PC0MLFD3\tlenovo(22/10/23)level 5 store
PC0JX993\tlenovo(22/10/23)level 5 store
PC0JXAK5\tlenovo(22/10/23)level 5 store
PC0MLG7V\tlenovo(22/10/23)level 5 store
PC0MLF9L\tlenovo(22/10/23)level 5 store
PC0V5GKU\tlenovo(22/10/23)level 5 store
PC0MLF92\tlenovo(22/10/23)level 5 store
PC0JX9X9\tlenovo(22/10/23)level 5 store
PC0JXB7T\tlenovo(22/10/23)level 5 store
PC0JXB91\tlenovo(22/10/23)level 5 store
PC0MLG7T\tlenovo(22/10/23)level 5 store
PC0MLJ7D\tlenovo(22/10/23)level 5 store
PC0MLG6R\tlenovo(22/10/23)level 5 store
PC0JXAL6\tlenovo(22/10/23)level 5 store
PC0MLJ6Y\tlenovo(22/10/23)level 5 store
PC0MLG82\tlenovo(22/10/23)level 5 store
PC0MLF8L\tlenovo(22/10/23)level 5 store
PC0MLFDB\tlenovo(22/10/23)level 5 store
PC0HKYCT\tlenovo(22/10/23)level 5 store
PC0MLG8N\tlenovo(22/10/23)level 5 store
PC0JX9A7\tlenovo(22/10/23)level 5 store
PC0MLHJT\tlenovo(22/10/23)level 5 store
PC0MLF93\tlenovo(22/10/23)level 5 store
PC0MLG8T\tlenovo(22/10/23)level 5 store
PC0MLG8Y\tlenovo(22/10/23)level 5 store
PC0MLG6H\tlenovo(22/10/23)level 5 store
PC0MLHK5\tlenovo(22/10/23)level 5 store
PC0MLF8P\tlenovo(22/10/23)level 5 store
PC0MLJ7T\tlenovo(22/10/23)level 5 store
PC0JX95N\tlenovo(22/10/23)level 5 store
PC0MLHJH\tlenovo(22/10/23)level 5 store
PC0MLFD4\tlenovo(22/10/23)level 5 store
PC0MLJ44\tlenovo(22/10/23)level 5 store
PC0HKYC5\tlenovo(22/10/23)level 5 store
PC0JXAH7\tlenovo(22/10/23)level 5 store
PC0MLJ3U\tlenovo(22/10/23)level 5 store
PC0MLFAD\tlenovo(22/10/23)level 5 store
PC0MLG7W\tlenovo(22/10/23)level 5 store
PC0MLG6Y\tlenovo(22/10/23)level 5 store
PC0MLJ6Z\tlenovo(22/10/23)level 5 store
PC0MLG8G\tlenovo(22/10/23)level 5 store
PC0JXAF4\tlenovo(22/10/23)level 5 store
PC0MLFBJ\tlenovo(22/10/23)level 5 store
PC0MLG8W\tlenovo(22/10/23)level 5 store
PC0JWTVS\tlenovo(22/10/23)level 5 store
PC0MLG9X\tlenovo(22/10/23)level 5 store
PC0MLJ4G\tlenovo(22/10/23)level 5 store
PC0MLJ5U\tlenovo(22/10/23)level 5 store
PC0MLJ6M\tlenovo(22/10/23)level 5 store
PC0MLF9C\tlenovo(22/10/23)level 5 store
PC0MLFC5\tlenovo(22/10/23)level 5 store
PC0JXAJM\tlenovo(22/10/23)level 5 store
PC0JXAFM\tlenovo(22/10/23)level 5 store
PC0MLGA6\tlenovo(22/10/23)level 5 store
PC0MLJ5M\tlenovo(22/10/23)level 5 store
R90WXJSM\tlenovo(22/10/23)level 5 store
PC0X87PT\tlenovo(22/10/23)level 5 store
PC0RHHF7\tlenovo(22/10/23)level 5 store
R90XJDW\tlenovo(22/10/23)level 5 store
PC0MLJ54\tlenovo(22/10/23)level 5 store
PC0MLHGP\tlenovo(22/10/23)level 5 store
PC0MLJ7F\tlenovo(22/10/23)level 5 store
PC0MLFAH\tlenovo(22/10/23)level 5 store
PC0MLHJA\tlenovo(22/10/23)level 5 store
PC0MLJ8B\tlenovo(22/10/23)level 5 store
PC0MLFCU\tlenovo(22/10/23)level 5 store
PC0MLFCD\tlenovo(22/10/23)level 5 store
PC0MLG5G\tlenovo(22/10/23)level 5 store
PC0MLF8Y\tlenovo(22/10/23)level 5 store
PC0MLJ81\tlenovo(22/10/23)level 5 store
PC0MLFA1\tlenovo(22/10/23)level 5 store
PC0M1F8C\tlenovo(22/10/23)level 5 store
PC0MLHGF\tlenovo(22/10/23)level 5 store
PC0MLJ7A\tlenovo(22/10/23)level 5 store
PC0MLF8W\tlenovo(22/10/23)level 5 store
PC0MLG7B\tlenovo(22/10/23)level 5 store
PC0MLF8X\tlenovo(22/10/23)level 5 store
PC0MLJ3Q\tlenovo(22/10/23)level 5 store
PC0MLFAN\tlenovo(22/10/23)level 5 store
PC0MLJ7W\tlenovo(22/10/23)level 5 store
PC0MLJ8E\tlenovo(22/10/23)level 5 store
PC0MLJ76\tlenovo(22/10/23)level 5 store
PC0JXAFP\tlenovo(22/10/23)level 5 store
PC0MLJ4R\tlenovo(22/10/23)level 5 store
PC0MLHK0\tlenovo(22/10/23)level 5 store
PC0MLHJD\tlenovo(22/10/23)level 5 store
PC0JX9VW\tlenovo(22/10/23)level 5 store
PC0MLF7V\tlenovo(22/10/23)level 5 store
PC0MLJ3V\tlenovo(22/10/23)level 5 store
PC0MLJ48\tlenovo(22/10/23)level 5 store
PC0MLG83\tlenovo(22/10/23)level 5 store
PC0HKYBG\tlenovo(22/10/23)level 5 store
PC0MLGAB\tlenovo(22/10/23)level 5 store
PC0MLF8T\tlenovo(22/10/23)level 5 store
PC0JX9WF\tlenovo(22/10/23)level 5 store
PC0MLJ87\tlenovo(22/10/23)level 5 store
PC0JX9XB\tlenovo(22/10/23)level 5 store
PC0MLJ5S\tlenovo(22/10/23)level 5 store
PF1BUJEB\tlenovo(22/10/23)level 5 store
PC0MLJ4D\tlenovo(22/10/23)level 5 store
PC0MLJ8C\tlenovo(22/10/23)level 5 store
YD02536C\tlenovo(22/10/23)level 5 store
5CG0107JLN\tHP(22/10/2025)level 5 store
5CD0128P91\tHP(22/10/2025)level 5 store
5CD0128P9J\tHP(22/10/2025)level 5 store
CND9481BN8\tHP(22/10/2025)level 5 store
CND9513ZSH\tHP(22/10/2025)level 5 store
5CG5325PPB\tHP(22/10/2025)level 5 store
CNU43491XK\tHP(22/10/2025)level 5 store
5CG5301W6M\tHP(22/10/2025)level 5 store
5CDO16D61X\tHP(22/10/2025)level 5 store
6355NW2\tDELL(23/10/2025)LEVEL 5 STORE
HK55NW2\tDELL(23/10/2025)LEVEL 5 STORE
5DS6NW2\tDELL(23/10/2025)LEVEL 5 STORE
2SX9H12\tDELL(23/10/2025)LEVEL 5 STORE
NXGGMSG001704052807600\tACER(23/10/2025)LEVEL 5 STORE
NXVPNSG00212423E187600\tACER(23/10/2025)LEVEL 5 STORE
NXGGMSG001704051FD7600\tACER(23/10/2025)LEVEL 5 STORE
NXGGMSG00170405538B7600\tACER(23/10/2025)LEVEL 5 STORE
NXGGMSG001704054797600\tACER(23/10/2025)LEVEL 5 STORE
DMPVG7S3HLF9\tIPAD(23/10/2025)LEVEL 5 STORE
GCTV30A4HP62\tIPAD(23/10/2025)LEVEL 5 STORE
5CG0102YHB\tHP BLUE DRAGANFLY(27/10/2025)LEVEL 5 KYLE CUBBOARD
5CG0102YH3\tHP BLUE DRAGANFLY(27/10/2025)LEVEL 5 KYLE CUBBOARD
5CG01474NK\tHP BLUE DRAGANFLY(27/10/2025)LEVEL 5 KYLE CUBBOARD
5CG0128229\tHP BLUE DRAGANFLY(27/10/2025)LEVEL 5 KYLE CUBBOARD
PC0JXAL3\tlenovo(23/10/23)level 5 store
IPY1VQ3\tDELL PRO support flex 2 in 1(23/10/2025)LEVEL 5 STORE
5CG0216W60\tLEVEL 5 STORE ROOM(18 nov 2025)
5CG1041SMK\tLEVEL 5 STORE ROOM(29 DEC 2025)BOX 1
5CG011523C6\tLEVEL 5 STORE ROOM(6 JAN 2026)BOX 1
1L55NW2\tLEVEL 5 STORE ROOM(6 JAN 2026)BOX 11
5CG011635N\tHP(23/12/2025)RETURNED TO VENDOR
5CG01474NP\tLEVEL 5 STORE ROOM(7 JAN 2026)BOX 11
9Z560J3(no ssd)\tCUPBOARD A 8 JAN 2026
NXGVZSG00D0240F7917600\tLEVEL 5 STORE ROOM(9 12 2025)
5CG5301S9W\tLEVEL 5 STORE ROOM(9 12 2025)
5CG50638NS\tLEVEL 5 STORE ROOM(9 12 2025)
5CD016D673\tLEVEL 5 STORE ROOM(9 12 2025)
5CG5325PP3\tLEVEL 5 STORE ROOM(9 12 2025)
NGGGMSG001704053177600\tLEVEL 5 STORE ROOM(9 12 2025)
PC0MLF8C\tLEVEL 5 STORE ROOM(9 12 2025)
PC0JX9W7\tLEVEL 5 STORE ROOM(9 12 2025)
PCOMLG8G\tLEVEL 5 STORE ROOM(9 12 2025)
ATFM-16006322\tLEVEL 5 STORE ROOM(9 12 2025)
PC0MLF8K\tLEVEL 5 STORE ROOM(9 12 2025)
0F33H9724043BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F368DQ24053BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F368F624053BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F3H8CX24023BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F33FPH24043BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F33H9X24043BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F368FJ24053BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F3H88M24023BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F368FM24053BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F3H8CC24023BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F3H88324023BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F3H88X24023BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F33HJG24043BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F3684824053BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F33HG824043BF\tLEVEL 5 STORE ROOM(9 12 2025)
0F3H88P24023BF\tLEVEL 5 STORE ROOM(9 12 2025)
5CG0102YH5\tLEVEL 5 STORE ROOM(22 1 2026)
5CG5320618\tLEVEL 5 STORE ROOM(26 1 2026)
R90WXJV9\tLEVEL 5 STORE ROOM(30 1 2026)
PC0MLJ5A\tLEVEL 5 STORE ROOM(6  2  2026)
B7RXG12\tLEVEL 5 STORE ROOM(6  2  2026)
5CG0107JLL\tLEVEL 5 STORE ROOM(26  2  2026)
0F33HGB24043BF\tIn Store
0F33H88J24023BF\tIn Store
0F33HG424043BF\tIn Store
0F3686H24053BF\tIn Store
0F3683D24053BF\tIn Store
0F3683C24053BF\tIn Store
0F3H88T24023BF\tIn Store
0F367YT24043BF\tIn Store
0F33HJH24043BF\tIn Store
0F3H87G24023BF\tIn Store
0F33HGK24043BF\tIn Store
0F368DY24043BF\tIn Store
0F3H89H24023BF\tIn Store
0F3H88W24023BF\tIn Store
0F3683324053BF\tIn Store
0F3684D24053BF\tIn Store
5CG012822C\tLEVEL 5 STORE ROOM(13 3 2026)
CX55NW2\tLEVEL 5 STORE ROOM(18 3 2026)
WFMHCYRFV9\tLEVEL 5 STORE ROOM (20 3 2026) IPAD PRO 3 GEN
MK893ZP/A\tAPPLE IPADMINI-64GB-LTE-23 MARCH 2026
DMPD201ALMTJ\tAPPLE IPADMINI-23 MARCH 2026
3V45NW2\tLEVEL 5 STORE ROOM(26 3 2026)
C02F42RUML7H\t
PF4AGMXG\t
R90XJFQE\t
R90XJFQL\t
NXGVSG0018500BD7D7600\t
CND9410PM4\t
PC0JX99X\t
R90XJDZH\t
5CG0107JLL\t
5CG542LBRF\t
NXHJZSG007947037364S00\t
NXKTXSG001401007624500\t
C12XHW3\t
2YMXHW3\t
7J6XHW3\t
CK3YSV3\t
JNBNTV3\t
20JZ9Y3\t
F18QR04\t
D5BL234\t
35DK234\tIn Store
BK33T8X24353M4\t
0F3H8CX24023BF\tIn Store
0F3H8CC24023BF\tIn Store
0F368FJ24053BF\tIn Store
0F368F624053BF\tIn Store
0F368FM24053BF\tIn Store
0F3H88324023BF\tIn Store
0F33HG824043BF\tIn Store
0F33HJG24043BF\tIn Store
0F3H88X24023BF\tIn Store
0F3683324053BF\t
0F3683D24053BF\tIn Store
0F3H88T24023BF\tIn Store
0F3H87G24023BF\tIn Store
0F3H88M24023BF\tIn Store
0F33HB324043BF\t
0F33HHF24043BF\tIn Store
0F33HJH24043BF\tIn Store
0F33H9724043BF\tIn Store
0F367YT24053BF\tIn Store
0F3684D24053BF\tIn Store
0F3683C24053BF\tIn Store
0F3H88W24023BF\tIn Store
0F3H89H24023BF\tIn Store
0F3H88P24023BF\tIn Store
0F3H88D24023BF\t
0F3H88J24023BF\t
0F3686H24053BF\tIn Store
0F368DY24053BF\t
0F33HGK24043BF\t
0F33HGB24043BF\tIn Store
0F33HG424043BF\tIn Store
0F33HGR24043BF\t
0F33H9X24043BF\tIn Store
0F33FPH24043BF\tIn Store
0F33HH624043BF\t
0F3684824053BF\tIn Store
0F3684724053BF\t
0F368DQ24053BF\tIn Store
0F367YY24053BF\t
0F3H87F24023BF\t
0F3H88924023BF\t
1CKXL34\t
2CKXL34\t
5B8XS34\t
65J2344\t
86D3674\t
76D3674\t
66D3674\t
DWSBB44\t
5CG4360J94\t
IYJ60J3\t
R90XJR6\t
"""

# ---------------------------------------------------------------------------
# Vendor / model inference
# ---------------------------------------------------------------------------
# Serial-number formats are vendor-specific and stable, so the model can be
# inferred where the status text doesn't name it. Order matters: the first
# matching rule wins.
SERIAL_PATTERNS = [
    (re.compile(r"^(5C[GD]|CN[DU])", re.I), "HP EliteBook (laptop)"),
    (re.compile(r"^(PC0|PCO|R90|R00|YD0|PF[14])", re.I), "Lenovo ThinkPad (laptop)"),
    (re.compile(r"^N[XG]{1,2}[A-Z0-9]{4}SG", re.I), "Acer TravelMate (laptop)"),
    (re.compile(r"^(DMP|GCT|WFM|MK8|C02)", re.I), "Apple iPad / MacBook (tablet)"),
    (re.compile(r"^0F3[0-9A-Z]{11}$", re.I), "Lenovo ThinkVision (monitor)"),
    (re.compile(r"^BK3[0-9A-Z]{11}$", re.I), "Lenovo ThinkVision (monitor)"),
    (re.compile(r"^ATFM-", re.I), "Docking station (accessory)"),
    (re.compile(r"^[A-Z0-9]{7}$", re.I), "Dell Latitude (laptop)"),
]

# Vendor names appearing in the free-text status column.
STATUS_MODEL_HINTS = [
    ("hp elitebook", "HP EliteBook (laptop)"),
    ("hp blue draganfly", "HP Elite Dragonfly (laptop)"),
    ("ipad pro", "Apple iPad Pro (tablet)"),
    ("ipadmini", "Apple iPad mini (tablet)"),
    ("ipad", "Apple iPad (tablet)"),
    ("dell pro support flex", "Dell Latitude 2-in-1 (laptop)"),
    ("dell flex", "Dell Latitude Flex (laptop)"),
    ("lenovo", "Lenovo ThinkPad (laptop)"),
    ("acer", "Acer TravelMate (laptop)"),
    ("dell", "Dell Latitude (laptop)"),
    ("hp", "HP EliteBook (laptop)"),
]

EMPLOYEE_ROLES = ["standard", "standard", "standard", "field", "executive", "developer"]

# ---------------------------------------------------------------------------
# Synthetic employee directory. Assets are assigned to EMP1000..EMP1399; each
# staff ID gets a stable name and an @ncs.com.sg address so either identifier
# can be typed into the portal's Employee ID field (identity_verification.py
# matches assignedUser OR assignedUserEmail).
#
# The domain matches the one the Pre-SignUp trigger enforces, so these double
# as valid sign-up addresses if you need portal logins for the same people.
# ---------------------------------------------------------------------------
EMPLOYEE_ID_MIN, EMPLOYEE_ID_MAX = 1000, 1399
EMAIL_DOMAIN = os.environ.get("TEST_EMAIL_DOMAIN", "ncs.com.sg")

_FIRST_NAMES = [
    "aisha", "arjun", "beatrice", "chandra", "daniel", "elaine", "farid", "grace",
    "hafiz", "irene", "jasmine", "kelvin", "lakshmi", "marcus", "nadia", "omar",
    "priya", "quentin", "rachel", "samuel", "tanvir", "ursula", "vikram", "wei",
    "xinyi", "yasmin", "zack", "adrian", "bernice", "clement",
]
_LAST_NAMES = [
    "tan", "lim", "wong", "kumar", "rahman", "chen", "goh", "ng", "singh", "lee",
    "ong", "raj", "koh", "teo", "yeo", "sim", "chua", "ho", "loh", "pang",
    "das", "menon", "quek", "seah", "toh", "wee", "yap", "zulkifli", "bala", "fong",
]


def build_employee_directory() -> dict[str, dict]:
    """staff ID -> {name, email}. Deterministic; collisions get a numeric suffix."""
    directory, used_emails = {}, set()
    for n in range(EMPLOYEE_ID_MIN, EMPLOYEE_ID_MAX + 1):
        emp_id = f"EMP{n}"
        h = int(hashlib.md5(emp_id.encode()).hexdigest(), 16)
        first = _FIRST_NAMES[h % len(_FIRST_NAMES)]
        last = _LAST_NAMES[(h >> 8) % len(_LAST_NAMES)]
        local = f"{first}.{last}"
        candidate, suffix = local, 1
        while candidate in used_emails:
            suffix += 1
            candidate = f"{local}{suffix}"
        used_emails.add(candidate)
        directory[emp_id] = {
            "assignedUserName": f"{first.capitalize()} {last.capitalize()}",
            "assignedUserEmail": f"{candidate}@{EMAIL_DOMAIN}",
        }
    return directory


EMPLOYEE_DIRECTORY = build_employee_directory()


def clean_serial(raw: str) -> tuple[str, str | None]:
    """Strips the parenthetical notes IT appends to the serial column.

    Returns (cleaned_serial, note). The API rejects anything outside
    [A-Za-z0-9-_], so '12337M3(DELL PRO SUPPORT FLEX)' has to become
    '12337M3' with the note carried separately, or the record is
    unreachable through /inspect.
    """
    note = None
    m = re.match(r"^([^(]+)\((.*)\)\s*$", raw.strip())
    if m:
        note = m.group(2).strip()
        raw = m.group(1)
    raw = raw.strip()

    # Apple part numbers carry a slash (MK893ZP/A). The slash is outside the
    # character set the API accepts, so the record would exist in the CMDB but
    # be permanently unreachable through /inspect. Store a normalised form and
    # keep the original in the note so the physical label still traces back.
    if "/" in raw:
        note = f"printed as {raw}" + (f"; {note}" if note else "")
        raw = raw.replace("/", "-")

    return raw, note


def map_status(status_text: str) -> str:
    s = status_text.lower()
    if not s.strip():
        return "Unknown"
    if "returned to vendor" in s:
        return "ReturnedToVendor"
    if "retired" in s:
        return "Retired"
    if any(w in s for w in ("store", "cupboard", "cubboard", "cubbard", "box", "r00m")):
        return "InStore"
    return "Unknown"


def infer_model(serial: str, status_text: str, note: str | None) -> tuple[str, bool]:
    """Returns (deviceModel, inferred). Status text wins over serial format."""
    haystack = f"{status_text} {note or ''}".lower()
    for needle, model in STATUS_MODEL_HINTS:
        if needle in haystack:
            return model, False
    for pattern, model in SERIAL_PATTERNS:
        if pattern.match(serial):
            return model, True
    return "Unknown Device (laptop)", True


def synth_attributes(serial: str) -> dict:
    """Deterministic per-serial attributes so reruns are reproducible."""
    h = int(hashlib.md5(serial.encode()).hexdigest(), 16)
    span = EMPLOYEE_ID_MAX - EMPLOYEE_ID_MIN + 1
    emp_id = f"EMP{EMPLOYEE_ID_MIN + (h % span)}"
    return {
        "assignedUser": emp_id,
        **EMPLOYEE_DIRECTORY[emp_id],
        "deviceAgeMonths": 6 + (h >> 8) % 54,      # 6..59
        "warrantyActive": ((h >> 16) % 100) < 35,  # ~35% in warranty
        "repairCount": [0, 0, 0, 1, 1, 2, 3, 4][(h >> 24) % 8],
        "employeeRole": EMPLOYEE_ROLES[(h >> 32) % len(EMPLOYEE_ROLES)],
    }


# ---------------------------------------------------------------------------
# Deliberate rule coverage. The damage score comes from the vision model (the
# photo), so the CMDB can only control the age/repair/warranty/role half of
# each rule — the "pair with" column says what photo to submit.
#
#   serial -> (overrides, lifecycle rule reached, photo severity to pair with)
# ---------------------------------------------------------------------------
RULE_COVERAGE = {
    # rule_2: Severe damage on a device past its role age threshold -> Dispose
    "5CG01523C7": ({"employeeRole": "executive", "deviceAgeMonths": 40,
                    "repairCount": 0, "warrantyActive": False},
                   "rule_2_severe_and_aged", "Severe"),
    # rule_3: >=3 prior repairs -> Refurbish (fires before rules 4/5)
    "PC0MLJ58":   ({"employeeRole": "standard", "deviceAgeMonths": 20,
                    "repairCount": 3, "warrantyActive": True},
                   "rule_3_excessive_repairs", "any (score <= 75)"),
    # rule_4a: Moderate damage, in warranty -> Repair at $90
    "6355NW2":    ({"employeeRole": "standard", "deviceAgeMonths": 12,
                    "repairCount": 0, "warrantyActive": True},
                   "rule_4a_repair", "Moderate"),
    # rule_4b: Severe damage out of warranty -> repair $1200 >= 60% of $1500
    "5CD0128PHV": ({"employeeRole": "standard", "deviceAgeMonths": 10,
                    "repairCount": 0, "warrantyActive": False},
                   "rule_4b_repair_uneconomical", "Severe"),
    # rule_5: Minor damage -> Continue Use
    "NXGGMSG001704052807600": ({"employeeRole": "standard", "deviceAgeMonths": 14,
                                "repairCount": 0, "warrantyActive": True},
                               "rule_5_minor_continue", "Minor"),
    # rule_6: aged executive device, no damage -> Refresh
    "DMPVG7S3HLF9": ({"employeeRole": "executive", "deviceAgeMonths": 44,
                      "repairCount": 0, "warrantyActive": False},
                     "rule_6_age_based_refresh", "None (pristine)"),
    # rule_1 needs a damage score > 75 and ignores every CMDB attribute, so any
    # record reaches it — submit a badly damaged photo against 5CG0216VSQ.
}


def build_records():
    seen_serials: dict[str, int] = {}
    records, rejected, duplicates = [], [], []
    counter = 0

    for line in RAW_INVENTORY.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        raw_serial = parts[0].strip()
        status_text = parts[1].strip() if len(parts) > 1 else ""
        if not raw_serial:
            continue

        serial, note = clean_serial(raw_serial)

        if not SERIAL_ALLOWED.fullmatch(serial):
            rejected.append((raw_serial, serial, "characters outside [A-Za-z0-9-_]"))
            continue

        seen_serials[serial] = seen_serials.get(serial, 0) + 1
        if seen_serials[serial] > 1:
            duplicates.append(serial)
            continue  # first occurrence wins; the CMDB serial GSI must be unique

        counter += 1
        model, inferred = infer_model(serial, status_text, note)
        record = {
            "assetId": f"ASSET-{counter:04d}",
            "serialNumber": serial,
            "deviceModel": model,
            "status": map_status(status_text),
            "sourceStatusText": status_text or "(blank)",
            "modelInferred": inferred,
            **synth_attributes(serial),
        }
        if note:
            record["sourceNote"] = note
        if serial in RULE_COVERAGE:
            overrides, rule, severity = RULE_COVERAGE[serial]
            record.update(overrides)
            record["testRule"] = rule
            record["testPairWithSeverity"] = severity
        records.append(record)

    return records, rejected, duplicates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help=f"write records to DynamoDB {TABLE_NAME} in {REGION}")
    ap.add_argument("--csv", metavar="PATH", help="write the derived records to CSV")
    ap.add_argument("--directory", metavar="PATH",
                    help="write the employee directory (staff ID, name, email, asset count) to CSV")
    args = ap.parse_args()

    records, rejected, duplicates = build_records()

    print(f"Parsed inventory -> {len(records)} unique CMDB records")
    print(f"  region/table       : {REGION} / {TABLE_NAME}")
    print(f"  duplicate serials  : {len(duplicates)} skipped")
    print(f"  rejected serials   : {len(rejected)} (API would 400 on these)")

    if rejected:
        print("\nSerials the API will reject as-is (sanitize_asset_id):")
        for raw, cleaned, why in rejected:
            print(f"  {raw!r} -> {why}")

    if duplicates:
        uniq = sorted(set(duplicates))
        print(f"\nDuplicate serials (first occurrence kept), {len(uniq)} distinct:")
        for s in uniq:
            print(f"  {s} x{duplicates.count(s) + 1}")

    print("\nLifecycle-rule coverage — ready-to-paste portal inputs.")
    print("Employee ID field accepts either the staff ID or the email.\n")
    for rec in records:
        if "testRule" in rec:
            print(f"  {rec['testRule']}")
            print(f"    Asset ID     : {rec['assetId']}")
            print(f"    Serial No.   : {rec['serialNumber']}")
            print(f"    Employee ID  : {rec['assignedUser']}   (or {rec['assignedUserEmail']})")
            print(f"    Device       : {rec['deviceModel']}")
            print(f"    Photo to use : {rec['testPairWithSeverity']} damage\n")

    by_status = Counter(r["status"] for r in records)
    print(f"\nStatus distribution: {dict(by_status)}")
    inferred = sum(1 for r in records if r["modelInferred"])
    print(f"Device model inferred from serial format (not stated): {inferred}/{len(records)}")

    if args.csv:
        cols = ["assetId", "serialNumber", "deviceModel", "status", "assignedUser",
                "assignedUserEmail", "assignedUserName",
                "deviceAgeMonths", "warrantyActive", "repairCount", "employeeRole",
                "modelInferred", "sourceStatusText"]
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)
        print(f"\nWrote {len(records)} rows to {args.csv}")

    if args.directory:
        holdings = Counter(r["assignedUser"] for r in records)
        rows = [
            {"employeeId": emp, "name": info["assignedUserName"],
             "email": info["assignedUserEmail"], "assetCount": holdings.get(emp, 0)}
            for emp, info in sorted(EMPLOYEE_DIRECTORY.items())
            if holdings.get(emp, 0) > 0
        ]
        with open(args.directory, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["employeeId", "name", "email", "assetCount"])
            w.writeheader()
            w.writerows(rows)
        print(f"Wrote {len(rows)} employees holding assets to {args.directory}")

    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to seed DynamoDB.")
        return 0

    import boto3
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    with table.batch_writer() as batch:
        for rec in records:
            batch.put_item(Item=rec)
    print(f"\nSeeded {len(records)} records into {TABLE_NAME} ({REGION}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
