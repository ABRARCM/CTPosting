import pandas as pd
import hashlib
from datetime import datetime

def stable_id(prefix, *parts):
    """Generate a stable ID from transaction data so Firebase keys survive regeneration."""
    raw = '|'.join(str(p).strip() for p in parts)
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:10]
    return f"{prefix}-{short_hash}"

# ============================================================
# CT BANK POSTING DASHBOARD
# Reads ALL CSVs from OneDrive CT Posting/Bank Information
#
# PPO vs Medicaid auto-detected by account:
#   Presto Den (6882381622) = PPO
#   PrestoDenZBA (6882381630 / 6882381633) = Medicaid
# ============================================================
import glob
import os
import json

ONEDRIVE_BASE = "/Users/Admin/Library/CloudStorage/OneDrive-ChildSmilesGroup,LLC(2)/ABRA RCM - CT/CT BILLING/CT Posting/Bank Information"
MONTH_FOLDER = "April 2026"
REPORTS_BUILDER = f"{ONEDRIVE_BASE}/Report Builder/{MONTH_FOLDER}"

# Derive month key (e.g., "2026-04") for file naming and Firebase namespacing
_month_names = {'January':'01','February':'02','March':'03','April':'04','May':'05','June':'06',
                'July':'07','August':'08','September':'09','October':'10','November':'11','December':'12'}
_mparts = MONTH_FOLDER.split()
MONTH_KEY = f"{_mparts[1]}-{_month_names[_mparts[0]]}"  # e.g., "2026-04"
MONTH_LABEL = MONTH_FOLDER  # e.g., "April 2026"
OUTPUT_DIR = "/Users/Admin/Desktop/Claude/CT BANK"

# Classify PPO vs Medicaid by destination account
def detect_source(row):
    to_acct = str(row.get('To Account Name', '')).strip()
    from_acct = str(row.get('From Account Name', '')).strip()
    to_num = str(row.get('To Account Number', '')).strip()
    from_num = str(row.get('From Account Number', '')).strip()
    # PrestoDenZBA = Medicaid (accounts ending 1630 or 1633)
    if 'ZBA' in to_acct or 'ZBA' in from_acct:
        return 'Medicaid'
    if any(num.endswith('1630') or num.endswith('1633') for num in [to_num, from_num]):
        return 'Medicaid'
    return 'PPO'

# Read ALL CSVs from OneDrive Reports Builder
all_frames = []
report_files = sorted(glob.glob(f"{REPORTS_BUILDER}/*.csv"), key=os.path.getmtime)
for rf in report_files:
    print(f"Reading OneDrive: {os.path.basename(rf)}")
    tmp = pd.read_csv(rf)
    tmp['_source'] = tmp.apply(detect_source, axis=1)
    all_frames.append(tmp)

if not all_frames:
    raise FileNotFoundError("No CSV files found in OneDrive Report Builder folder")

df = pd.concat(all_frames, ignore_index=True)

# Deduplicate — same date + amount + from account + ACH ID = same transaction
df['_dedup_key'] = (
    df['Date'].astype(str) + '|' +
    df['Amount'].astype(str) + '|' +
    df['From Account Name'].astype(str).str.strip() + '|' +
    df['ACH Individual ID'].astype(str).str.strip()
)
before = len(df)
df = df.drop_duplicates(subset='_dedup_key', keep='first')
dupes = before - len(df)
if dupes > 0:
    print(f"Removed {dupes} duplicate transactions")
df = df.drop(columns=['_dedup_key'])

# Clean columns
df.columns = df.columns.str.strip()
df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce')
df['Date'] = pd.to_datetime(df['Date'])
df['From Account Name'] = df['From Account Name'].astype(str).str.strip()
df['To Account Name'] = df['To Account Name'].astype(str).str.strip()
df['ACH Individual ID'] = df['ACH Individual ID'].astype(str).str.strip()
df['ACH Description'] = df['ACH Description'].astype(str).str.strip() if 'ACH Description' in df.columns else ''
df['ACH Entry Description'] = df['ACH Entry Description'].astype(str).str.strip() if 'ACH Entry Description' in df.columns else ''
df['Description'] = df['Description'].astype(str).str.strip() if 'Description' in df.columns else ''
df['Payment Method'] = df['Payment Method'].astype(str).str.strip() if 'Payment Method' in df.columns else ''

# Exclude ONLY funding transfers between accounts (not all Other Transactions)
# CT has "Other Transactions" that are legitimate deposits (wire transfers, etc.)
if 'Payment Method' in df.columns and 'Description' in df.columns:
    is_funding_transfer = (
        (df['Payment Method'] == 'Other Transactions') &
        (df['Description'].str.contains('FUNDING TRANSFER|TRANSFER FROM|TRANSFER TO', case=False, na=False))
    )
    num_transfers = is_funding_transfer.sum()
    if num_transfers > 0:
        print(f"Excluded {num_transfers} internal transfers between accounts")
    df = df[~is_funding_transfer]

incoming = df[df['Amount'] > 0].copy()
outgoing = df[df['Amount'] < 0].copy()

# CT deposit sources — card processors
deposit_sources = ['MERCHANT BANKCD', 'GLOBAL PAYMENTS', 'SYNCHRONY BANK']

def classify(row):
    name = str(row['From Account Name']).strip()
    payment_method = str(row.get('Payment Method', '')).strip()
    if name in deposit_sources:
        return 'Deposits'
    if payment_method == 'Check':
        return 'Check Deposits'
    if payment_method == 'Other Transactions':
        return 'Cash Deposits'
    return 'EFT'

incoming['Category'] = incoming.apply(classify, axis=1)
incoming = incoming.sort_values(['Date', 'Amount'], ascending=[False, False])

# Map ACH descriptions to friendly location names
def friendly_name(row):
    from_name = str(row['From Account Name']).strip()
    ach_desc = str(row.get('ACH Description', '')).strip()

    if from_name == 'MERCHANT BANKCD':
        if 'ALL ABOUT KIDS DANBURY' in ach_desc: return 'Danbury'
        if 'ALL ABOUT KIDS DERBY' in ach_desc: return 'Derby'
        if 'ALL ABOUT KIDS NORWALK' in ach_desc: return 'Norwalk'
        if 'ALL ABOUT KIDS STAMFOR' in ach_desc: return 'Stamford'
        if 'BRIDGEFORT' in ach_desc: return 'Bridgeport'
        if 'PRESTO DENTAL NORWALK' in ach_desc: return 'Norwalk (Presto)'
        return ach_desc if ach_desc and ach_desc != 'nan' else from_name

    if from_name == 'GLOBAL PAYMENTS':
        ach_id = str(row['ACH Individual ID']).strip()
        if '8788240117293' in ach_id: return 'Global Payments (Main)'
        if '8788240117275' in ach_id: return 'Global Payments (GP)'
        if '8788240117296' in ach_id: return 'Global Payments (Ortho)'
        if '8788240117299' in ach_id: return 'Global Payments (Other)'
        return f'Global Payments ({ach_id})'

    if from_name == 'SYNCHRONY BANK':
        return 'Synchrony (CareCredit)'

    # Insurance EFTs
    if 'CIGNA' in from_name:
        return 'Cigna'
    if 'ST OF CONN' in from_name:
        return 'CT Medicaid (DSS)'

    return from_name if from_name and from_name != 'nan' else 'Unknown'

incoming['Payer'] = incoming.apply(friendly_name, axis=1)

def deposit_type_label(row):
    entry = row.get('ACH Entry Description', '')
    if 'DEPOSIT' in str(entry): return 'Deposit'
    if 'GLOBAL DEP' in str(entry): return 'Global Dep'
    if 'MTOT' in str(entry): return 'Monthly'
    if 'HCCLAIMPMT' in str(entry): return 'Claim Pmt'
    return str(entry)[:15] if entry and str(entry) != 'nan' else ''

incoming['DepositType'] = incoming.apply(deposit_type_label, axis=1)

def fmt_money(val):
    return f"${val:,.2f}"

deposits = incoming[incoming['Category'] == 'Deposits']
check_dep = incoming[incoming['Category'] == 'Check Deposits']
cash_dep = incoming[incoming['Category'] == 'Cash Deposits']
eft_all = incoming[incoming['Category'] == 'EFT']
eft = eft_all[eft_all['_source'] == 'PPO']
eft_medicaid = eft_all[eft_all['_source'] == 'Medicaid']

total_deposits = deposits['Amount'].sum()
total_check_dep = check_dep['Amount'].sum()
total_cash_dep = cash_dep['Amount'].sum()
total_eft = eft['Amount'].sum()
total_eft_medicaid = eft_medicaid['Amount'].sum()
total_outgoing_all = outgoing['Amount'].sum()
total_incoming = incoming['Amount'].sum()
net_total = total_incoming + total_outgoing_all

date_min = df['Date'].min().strftime('%m/%d/%Y')
date_max = df['Date'].max().strftime('%m/%d/%Y')

# === Build overview rows ===
def overview_rows(data, category):
    grouped = data.groupby(data['Date'].dt.strftime('%Y-%m-%d'))
    rows = ""
    for date_key in sorted(grouped.groups.keys(), reverse=True):
        group = grouped.get_group(date_key)
        date_display = datetime.strptime(date_key, '%Y-%m-%d').strftime('%m/%d/%Y')
        total = group['Amount'].sum()
        count = len(group)
        rows += f"""<tr>
            <td>{date_display}</td>
            <td><span class="cat-badge cat-{category.lower().replace(' ','')}">{category}</span></td>
            <td class="count-col">{count}</td>
            <td class="amount">{fmt_money(abs(total))}</td>
        </tr>"""
    return rows

def overview_out_rows(data):
    grouped = data.groupby(data['Date'].dt.strftime('%Y-%m-%d'))
    rows = ""
    for date_key in sorted(grouped.groups.keys(), reverse=True):
        group = grouped.get_group(date_key)
        date_display = datetime.strptime(date_key, '%Y-%m-%d').strftime('%m/%d/%Y')
        total = group['Amount'].sum()
        count = len(group)
        rows += f"""<tr>
            <td>{date_display}</td>
            <td><span class="cat-badge cat-outgoing">Outgoing</span></td>
            <td class="count-col">{count}</td>
            <td class="amount negative">({fmt_money(abs(total))})</td>
        </tr>"""
    return rows

overview_deposit_rows = overview_rows(deposits, 'Deposits')
overview_eft_rows = overview_rows(eft, 'EFT')
overview_outgoing_rows = overview_out_rows(outgoing)

# === Build detail rows with Posted dropdown ===
def detail_deposit_rows(data):
    grouped = data.groupby(data['Date'].dt.strftime('%Y-%m-%d'))
    html = ""
    date_keys = sorted(grouped.groups.keys(), reverse=True)
    for date_key in date_keys:
        group = grouped.get_group(date_key).sort_values('Amount', ascending=False)
        date_display = datetime.strptime(date_key, '%Y-%m-%d').strftime('%m/%d/%Y')
        date_total = group['Amount'].sum()
        date_count = len(group)
        html += f"""<tr class="date-header">
            <td colspan="5">
                <span class="date-label">{date_display}</span>
                <span class="date-stats">{date_count} deposits &bull; <strong>{fmt_money(date_total)}</strong></span>
            </td>
        </tr>"""
        for _, row in group.iterrows():
            html += f"""<tr>
            <td class="date-col">{date_display}</td>
            <td><span class="payer-badge deposit-badge">{row['Payer']}</span></td>
            <td class="type-col">{row['DepositType']}</td>
            <td class="amount">{fmt_money(row['Amount'])}</td>
            <td class="ach-col">{row['ACH Individual ID']}</td>
        </tr>"""
    return html

def detail_eft_rows(data):
    grouped = data.groupby(data['Date'].dt.strftime('%Y-%m-%d'))
    html = ""
    date_keys = sorted(grouped.groups.keys(), reverse=True)
    for date_key in date_keys:
        group = grouped.get_group(date_key).sort_values('Amount', ascending=False)
        date_display = datetime.strptime(date_key, '%Y-%m-%d').strftime('%m/%d/%Y')
        date_total = group['Amount'].sum()
        date_count = len(group)
        html += f"""<tr class="date-header">
            <td colspan="8">
                <span class="date-label">{date_display}</span>
                <span class="date-stats">{date_count} EFTs &bull; <strong>{fmt_money(date_total)}</strong></span>
            </td>
        </tr>"""
        for _, row in group.iterrows():
            sid = stable_id('eft', date_key, row['From Account Name'], row['Amount'], row['ACH Individual ID'])
            html += f"""<tr data-row="{sid}">
            <td class="date-col">{date_display}</td>
            <td><span class="payer-badge eft-badge">{row.get('Payer', row['From Account Name'])}</span></td>
            <td class="amount">{fmt_money(row['Amount'])}</td>
            <td class="ach-col">{row['ACH Individual ID']}</td>
            <td class="desc-col">{row['ACH Description']}</td>
            <td class="posted-col">
                <select class="posted-select eob-select" data-row="eob-{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                </select>
            </td>
            <td class="posted-col">
                <select class="posted-select" data-row="{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                    <option value="partial">Partial</option>
                </select>
            </td>
            <td class="remarks-col">
                <input type="text" class="remarks-input" data-row="rmk-{sid}" placeholder="Add remarks..." onchange="saveRemark(this)">
            </td>
        </tr>"""
    return html

def detail_check_deposit_rows(data):
    if data.empty:
        return ""
    grouped = data.groupby(data['Date'].dt.strftime('%Y-%m-%d'))
    html = ""
    date_keys = sorted(grouped.groups.keys(), reverse=True)
    for date_key in date_keys:
        group = grouped.get_group(date_key).sort_values('Amount', ascending=False)
        date_display = datetime.strptime(date_key, '%Y-%m-%d').strftime('%m/%d/%Y')
        date_total = group['Amount'].sum()
        date_count = len(group)
        num_items = group['Total Number of Items'].astype(str).str.strip()
        total_items = sum(int(float(x)) for x in num_items if x and x != 'nan')
        html += f"""<tr class="date-header">
            <td colspan="7">
                <span class="date-label">{date_display}</span>
                <span class="date-stats">{date_count} deposits ({total_items} checks) &bull; <strong>{fmt_money(date_total)}</strong></span>
            </td>
        </tr>"""
        for _, row in group.iterrows():
            items = str(row.get('Total Number of Items', ''))
            items = items if items and items != 'nan' else ''
            sid = stable_id('chk', date_key, row['Amount'], items)
            html += f"""<tr data-row="{sid}">
            <td class="date-col">{date_display}</td>
            <td><span class="payer-badge deposit-badge">Check Deposit</span></td>
            <td class="amount">{fmt_money(row['Amount'])}</td>
            <td class="count-col">{items} checks</td>
            <td class="posted-col">
                <select class="posted-select eob-select" data-row="eob-{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                </select>
            </td>
            <td class="posted-col">
                <select class="posted-select" data-row="{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                    <option value="partial">Partial</option>
                </select>
            </td>
            <td class="remarks-col">
                <input type="text" class="remarks-input" data-row="rmk-{sid}" placeholder="Add remarks..." onchange="saveRemark(this)">
            </td>
        </tr>"""
    return html

def detail_cash_deposit_rows(data):
    if data.empty:
        return ""
    grouped = data.groupby(data['Date'].dt.strftime('%Y-%m-%d'))
    html = ""
    date_keys = sorted(grouped.groups.keys(), reverse=True)
    for date_key in date_keys:
        group = grouped.get_group(date_key).sort_values('Amount', ascending=False)
        date_display = datetime.strptime(date_key, '%Y-%m-%d').strftime('%m/%d/%Y')
        date_total = group['Amount'].sum()
        date_count = len(group)
        html += f"""<tr class="date-header">
            <td colspan="7">
                <span class="date-label">{date_display}</span>
                <span class="date-stats">{date_count} deposits &bull; <strong>{fmt_money(date_total)}</strong></span>
            </td>
        </tr>"""
        for _, row in group.iterrows():
            desc = str(row.get('Description', ''))[:60] if pd.notna(row.get('Description')) else ''
            sid = stable_id('cash', date_key, row['Amount'], desc[:30])
            html += f"""<tr data-row="{sid}">
            <td class="date-col">{date_display}</td>
            <td><span class="payer-badge cash-badge">Cash Deposit</span></td>
            <td class="amount">{fmt_money(row['Amount'])}</td>
            <td class="desc-col">{desc}</td>
            <td class="posted-col">
                <select class="posted-select" data-row="{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                    <option value="partial">Partial</option>
                </select>
            </td>
            <td class="remarks-col">
                <input type="text" class="remarks-input" data-row="rmk-{sid}" placeholder="Add remarks..." onchange="saveRemark(this)">
            </td>
        </tr>"""
    return html

def detail_outgoing_rows(data):
    data = data.sort_values(['Date', 'Amount'], ascending=[False, True])
    grouped = data.groupby(data['Date'].dt.strftime('%Y-%m-%d'))
    html = ""
    date_keys = sorted(grouped.groups.keys(), reverse=True)
    for date_key in date_keys:
        group = grouped.get_group(date_key).sort_values('Amount')
        date_display = datetime.strptime(date_key, '%Y-%m-%d').strftime('%m/%d/%Y')
        date_total = group['Amount'].sum()
        date_count = len(group)
        html += f"""<tr class="date-header">
            <td colspan="6">
                <span class="date-label">{date_display}</span>
                <span class="date-stats">{date_count} debits &bull; <strong class="negative">({fmt_money(abs(date_total))})</strong></span>
            </td>
        </tr>"""
        for _, row in group.iterrows():
            to_name = row['To Account Name'] if pd.notna(row.get('To Account Name')) else ''
            sid = stable_id('out', date_key, to_name, row['Amount'], row['ACH Individual ID'])
            html += f"""<tr data-row="{sid}">
            <td class="date-col">{date_display}</td>
            <td><span class="payer-badge outgoing-badge">{to_name}</span></td>
            <td class="amount negative">({fmt_money(abs(row['Amount']))})</td>
            <td class="ach-col">{row['ACH Individual ID']}</td>
            <td class="desc-col">{row['ACH Entry Description'] if pd.notna(row.get('ACH Entry Description')) else ''}</td>
            <td class="remarks-col">
                <input type="text" class="remarks-input" data-row="rmk-{sid}" placeholder="Add remarks..." onchange="saveRemark(this)">
            </td>
        </tr>"""
    return html

# === LOCKBOX CSV DATA ===
ONEDRIVE_LOCKBOX = f"{ONEDRIVE_BASE}/LockBox"

lb_frames = []
for lb_path in [ONEDRIVE_LOCKBOX]:
    lb_files = sorted(glob.glob(f"{lb_path}/*.csv"))
    for lf in lb_files:
        print(f"Reading Lockbox: {os.path.basename(lf)}")
        tmp = pd.read_csv(lf)
        tmp.columns = tmp.columns.str.strip()
        lb_frames.append(tmp)

if lb_frames:
    lb = pd.concat(lb_frames, ignore_index=True)
    lb['Amount'] = pd.to_numeric(lb['Amount'], errors='coerce')
    lb['Processed Date'] = pd.to_datetime(lb['Processed Date'], format='%Y%m%d')
    lb['Lockbox Number'] = lb['Lockbox Number'].astype(str).str.strip()
    lb['Item Type'] = lb['Item Type'].astype(str).str.strip()
    # Only use Check rows
    lb = lb[(lb['Item Type'] == 'Check') & (lb['Amount'] > 0)]
    if 'Transaction ID' in lb.columns:
        before_lb = len(lb)
        lb = lb.drop_duplicates(subset='Transaction ID', keep='first')
        lb_dupes = before_lb - len(lb)
        if lb_dupes > 0:
            print(f"Removed {lb_dupes} duplicate lockbox rows")
    lb_checks = lb.copy()
    lb_checks['Check Number'] = lb_checks['Check Number'].apply(
        lambda x: str(int(float(x))) if pd.notna(x) and str(x).strip() not in ['', 'nan'] else ''
    )
    lb_checks = lb_checks.sort_values(['Processed Date', 'Amount'], ascending=[False, False])
else:
    lb_checks = pd.DataFrame()
    print("No lockbox CSV files found (folder empty)")

# CT uses lockbox #11245 for all checks
lb_all = lb_checks
total_lb_all = lb_all['Amount'].sum() if not lb_all.empty else 0

def detail_lockbox_detail_rows(data, prefix):
    if data.empty:
        return ""
    grouped = data.groupby(data['Processed Date'].dt.strftime('%Y-%m-%d'))
    html = ""
    date_keys = sorted(grouped.groups.keys(), reverse=True)
    for date_key in date_keys:
        group = grouped.get_group(date_key).sort_values('Amount', ascending=False)
        date_display = datetime.strptime(date_key, '%Y-%m-%d').strftime('%m/%d/%Y')
        date_total = group['Amount'].sum()
        date_count = len(group)
        html += f"""<tr class="date-header">
            <td colspan="6">
                <span class="date-label">{date_display}</span>
                <span class="date-stats">{date_count} checks &bull; <strong>{fmt_money(date_total)}</strong></span>
            </td>
        </tr>"""
        for _, row in group.iterrows():
            chk = row['Check Number'] if pd.notna(row['Check Number']) and row['Check Number'] != 'nan' else ''
            sid = stable_id(prefix, date_key, row['Amount'], chk, row.get('Transaction ID', ''))
            html += f"""<tr data-row="{sid}">
            <td class="date-col">{date_display}</td>
            <td class="check-col">{chk}</td>
            <td class="amount">{fmt_money(row['Amount'])}</td>
            <td class="posted-col">
                <select class="posted-select eob-select" data-row="eob-{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                </select>
            </td>
            <td class="posted-col">
                <select class="posted-select" data-row="{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                    <option value="partial">Partial</option>
                </select>
            </td>
            <td class="remarks-col">
                <input type="text" class="remarks-input" data-row="rmk-{sid}" placeholder="Add remarks..." onchange="saveRemark(this)">
            </td>
        </tr>"""
    return html

lb_all_rows = detail_lockbox_detail_rows(lb_all, 'lb')

# === BANK GENERAL (STATEMENT) DATA — Credits only, no transfers ===
def load_bank_general(path, acct_name):
    bg = pd.read_csv(path, names=['DATE','TYPE','DESCRIPTION','AMOUNT','BALANCE','_extra'], skiprows=1)
    bg = bg.drop(columns=['_extra'], errors='ignore')
    bg['AMOUNT'] = pd.to_numeric(bg['AMOUNT'], errors='coerce')
    bg['DATE'] = pd.to_datetime(bg['DATE'])
    bg['DESCRIPTION'] = bg['DESCRIPTION'].astype(str)
    bg['TYPE'] = bg['TYPE'].astype(str).str.strip()
    bg['ACCT'] = acct_name
    bg = bg[bg['AMOUNT'] > 0].copy()
    def classify_bg(desc):
        d = desc.upper()
        if 'BANKCARD' in d or 'MERCHANT BANKCD' in d or 'SYNCHRONY' in d or 'GLOBAL PAYMENTS' in d:
            return 'Card Deposits'
        if 'LOCKBOX' in d:
            return 'Lockbox'
        if 'FUNDING TRANSFER' in d:
            return 'Transfer'
        if d.startswith('DEPOSIT') and 'LOCKBOX' not in d:
            return 'Deposit'
        if 'CIGNA' in d or 'HCCLAIMPMT' in d:
            return 'EFT'
        return 'EFT'
    bg['BG_CAT'] = bg['DESCRIPTION'].apply(classify_bg)
    bg = bg[bg['BG_CAT'] != 'Transfer'].copy()
    return bg

ONEDRIVE_GENERAL = f"{ONEDRIVE_BASE}/General Statement/{MONTH_FOLDER}"

def find_bank_general(acct_num, acct_label):
    files = sorted(glob.glob(f"{ONEDRIVE_GENERAL}/*{acct_num}*.csv"), key=os.path.getmtime)
    if not files:
        print(f"Warning: No Bank General file found for {acct_label} ({acct_num})")
        return pd.DataFrame(columns=['DATE','TYPE','DESCRIPTION','AMOUNT','BALANCE','ACCT','BG_CAT'])
    frames = []
    for f in files:
        print(f"Reading Bank General ({acct_label}): {os.path.basename(f)}")
        frames.append(load_bank_general(f, acct_label))
    combined = pd.concat(frames, ignore_index=True)
    combined['_bg_dedup'] = combined['DATE'].astype(str) + '|' + combined['AMOUNT'].astype(str)
    before = len(combined)
    combined = combined.drop_duplicates(subset='_bg_dedup', keep='first')
    dupes = before - len(combined)
    if dupes > 0:
        print(f"  Removed {dupes} duplicate Bank General rows")
    combined = combined.drop(columns=['_bg_dedup'])
    return combined

bg_ppo = find_bank_general('6882381622', 'PPO')
bg_med = find_bank_general('6882381633', 'Medicaid')
# Also try 1630 if 1633 not found
if bg_med.empty:
    bg_med = find_bank_general('6882381630', 'Medicaid')

# === DEPOSITED CHECKS (from Deposited Checks folder) ===
DEPOSITED_CHECKS_PATH = f"{ONEDRIVE_BASE}/Deposited Checks/{MONTH_FOLDER}"
dep_check_files = sorted(glob.glob(f"{DEPOSITED_CHECKS_PATH}/*.csv"), key=os.path.getmtime, reverse=True) if os.path.exists(DEPOSITED_CHECKS_PATH) else []

import re as _re

all_dep_checks = []
for dcf in dep_check_files:
    try:
        dc = pd.read_csv(dcf)
        dc.columns = dc.columns.str.strip()
        fname = os.path.basename(dcf)
        date_match = _re.match(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', fname)
        deposit_date = ''
        if date_match:
            m, d, y = date_match.groups()
            deposit_date = f"{m.zfill(2)}/{d.zfill(2)}/{y}"
        slip_row = dc[dc['Item'] == 'Deposit Slip']
        deposit_total = 0
        deposit_acct = ''
        if not slip_row.empty:
            amt_str = str(slip_row.iloc[0].get('Amount', '0'))
            amt_str = amt_str.replace('"', '').replace(',', '')
            deposit_total = float(amt_str) if amt_str else 0
            deposit_acct = str(slip_row.iloc[0].get('To Account Number', ''))
        checks = dc[dc['Item'] == 'Check'].copy()
        checks['_deposit_total'] = deposit_total
        checks['_deposit_acct'] = deposit_acct
        checks['_deposit_date'] = deposit_date
        checks['_source_file'] = os.path.basename(dcf)
        checks['Amount'] = checks['Amount'].astype(str).str.replace('"', '').str.replace(',', '')
        checks['Amount'] = pd.to_numeric(checks['Amount'], errors='coerce')
        checks['Check #'] = checks['Check #'].astype(str).str.strip()
        all_dep_checks.append(checks)
        print(f"Reading Deposited Check: {os.path.basename(dcf)}")
    except Exception as e:
        print(f"Warning: Could not read {dcf}: {e}")

if all_dep_checks:
    dep_checks_df = pd.concat(all_dep_checks, ignore_index=True)
    dep_checks_df = dep_checks_df.sort_values('Amount', ascending=False)
else:
    dep_checks_df = pd.DataFrame()
    print("No deposited check files found")

total_dep_checks = dep_checks_df['Amount'].sum() if not dep_checks_df.empty else 0
num_dep_checks = len(dep_checks_df)

def detail_deposited_check_rows(data):
    if data.empty:
        return ""
    html = ""
    files_sorted = sorted(data['_source_file'].unique(), key=lambda f: data[data['_source_file']==f]['_deposit_date'].iloc[0], reverse=True)
    for source_file in files_sorted:
        group = data[data['_source_file'] == source_file].sort_values('Amount', ascending=False)
        slip_total = group['_deposit_total'].iloc[0]
        slip_acct = group['_deposit_acct'].iloc[0]
        slip_date = group['_deposit_date'].iloc[0]
        acct_label = 'Medicaid' if any(x in str(slip_acct) for x in ['1630', '1633']) else 'PPO'
        count = len(group)
        html += f"""<tr class="date-header">
            <td colspan="9">
                <span class="date-label">{slip_date} — Deposit Slip — {acct_label}</span>
                <span class="date-stats">{count} checks &bull; Slip Total: <strong>{fmt_money(slip_total)}</strong></span>
            </td>
        </tr>"""
        for _, row in group.iterrows():
            chk_num = row['Check #'] if pd.notna(row['Check #']) and row['Check #'] != 'nan' else ''
            from_acct = str(row.get('From Account', '')) if pd.notna(row.get('From Account')) else ''
            routing = str(row.get('Routing Number', '')) if pd.notna(row.get('Routing Number')) else ''
            dep_date = row.get('_deposit_date', '')
            sid = stable_id('depchk', chk_num, row['Amount'], from_acct)
            html += f"""<tr data-row="{sid}">
            <td class="date-col">{dep_date}</td>
            <td class="check-col">{chk_num}</td>
            <td class="amount">{fmt_money(row['Amount'])}</td>
            <td class="ach-col">{from_acct}</td>
            <td class="ach-col">{routing}</td>
            <td class="posted-col">
                <select class="posted-select eob-select" data-row="eob-{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                </select>
            </td>
            <td class="posted-col">
                <select class="posted-select" data-row="{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                    <option value="partial">Partial</option>
                </select>
            </td>
            <td class="remarks-col">
                <input type="text" class="remarks-input" data-row="rmk-{sid}" placeholder="Add remarks..." onchange="saveRemark(this)">
            </td>
        </tr>"""
    return html

dep_check_rows_html = detail_deposited_check_rows(dep_checks_df)

# Build PPO overview (EFT + Lockbox + Deposited Checks)
def build_ppo_overview(bg_data, dep_checks_data):
    if bg_data.empty:
        return "", 0, 0, 0, 0
    bg_data = bg_data.sort_values('DATE', ascending=False)
    dates = sorted(bg_data['DATE'].dt.strftime('%Y-%m-%d').unique(), reverse=True)
    rows = ""
    grand_eft = 0
    grand_lb = 0
    grand_chk = 0
    grand_total = 0
    for dt in dates:
        day = bg_data[bg_data['DATE'].dt.strftime('%Y-%m-%d') == dt]
        date_display = datetime.strptime(dt, '%Y-%m-%d').strftime('%m/%d/%Y')
        day_eft = day[day['BG_CAT'] == 'EFT']['AMOUNT'].sum()
        day_lb = day[day['BG_CAT'] == 'Lockbox']['AMOUNT'].sum()
        day_dep = day[day['BG_CAT'] == 'Deposit']['AMOUNT'].sum()
        day_total = day_eft + day_lb + day_dep
        grand_eft += day_eft
        grand_lb += day_lb
        grand_chk += day_dep
        grand_total += day_total
        rows += f"""<tr>
            <td class="date-col" style="font-weight:600;color:#0d4f3c">{date_display}</td>
            <td class="amount">{fmt_money(day_eft) if day_eft > 0 else '—'}</td>
            <td class="amount" style="color:#A855F7">{fmt_money(day_lb) if day_lb > 0 else '—'}</td>
            <td class="amount" style="color:#e65100">{fmt_money(day_dep) if day_dep > 0 else '—'}</td>
            <td class="amount" style="font-weight:800">{fmt_money(day_total)}</td>
        </tr>"""
    rows += f"""<tr class="total-row">
        <td><strong>TOTAL</strong></td>
        <td class="amount"><strong>{fmt_money(grand_eft)}</strong></td>
        <td class="amount" style="color:#A855F7"><strong>{fmt_money(grand_lb)}</strong></td>
        <td class="amount" style="color:#e65100"><strong>{fmt_money(grand_chk)}</strong></td>
        <td class="amount" style="font-weight:800"><strong>{fmt_money(grand_total)}</strong></td>
    </tr>"""
    return rows, grand_eft, grand_lb, grand_chk, grand_total

ppo_overview_rows, ppo_eft, ppo_lb, ppo_chk, ppo_total = build_ppo_overview(bg_ppo, dep_checks_df)

# Build Medicaid overview (EFT only — no lockbox in CT Medicaid)
def build_med_overview_from_eft(eft_med_data):
    if eft_med_data.empty:
        return "", 0
    grouped = eft_med_data.groupby(eft_med_data['Date'].dt.strftime('%Y-%m-%d'))
    rows = ""
    grand_total = 0
    for date_key in sorted(grouped.groups.keys(), reverse=True):
        group = grouped.get_group(date_key)
        date_display = datetime.strptime(date_key, '%Y-%m-%d').strftime('%m/%d/%Y')
        day_total = group['Amount'].sum()
        count = len(group)
        grand_total += day_total
        rows += f"""<tr>
            <td class="date-col" style="font-weight:600;color:#0d4f3c">{date_display}</td>
            <td class="count-col">{count}</td>
            <td class="amount" style="font-weight:800">{fmt_money(day_total)}</td>
        </tr>"""
    rows += f"""<tr class="total-row">
        <td><strong>TOTAL</strong></td>
        <td class="count-col"><strong>{len(eft_med_data)}</strong></td>
        <td class="amount" style="font-weight:800"><strong>{fmt_money(grand_total)}</strong></td>
    </tr>"""
    return rows, grand_total

med_overview_rows, med_total = build_med_overview_from_eft(eft_medicaid)

# Bank deposits reconciliation
def get_bank_deposits(bg_data, lockbox_num, acct_label):
    if bg_data.empty:
        return pd.DataFrame()
    deposits_lb = bg_data[bg_data['BG_CAT'] == 'Lockbox'].copy()
    deposits_other = bg_data[bg_data['BG_CAT'] == 'Deposit'].copy()
    all_dep = pd.concat([deposits_lb, deposits_other], ignore_index=True)
    all_dep = all_dep.sort_values('DATE', ascending=False)
    all_dep['LB_NUM'] = lockbox_num
    all_dep['ACCT_LABEL'] = acct_label
    return all_dep

bank_dep_ppo = get_bank_deposits(bg_ppo, '11245', 'PPO')

def build_bank_deposit_rows(bank_deps, lb_detail, lockbox_num, prefix):
    if bank_deps.empty:
        return ""
    html = ""
    bank_deps = bank_deps.sort_values('DATE', ascending=False)
    dates = sorted(bank_deps['DATE'].dt.strftime('%Y-%m-%d').unique(), reverse=True)
    for dt in dates:
        day = bank_deps[bank_deps['DATE'].dt.strftime('%Y-%m-%d') == dt]
        date_display = datetime.strptime(dt, '%Y-%m-%d').strftime('%m/%d/%Y')
        day_lb = day[day['BG_CAT'] == 'Lockbox']
        day_chk = day[day['BG_CAT'] == 'Deposit']
        lb_bank_total = day_lb['AMOUNT'].sum()
        chk_total = day_chk['AMOUNT'].sum()
        # Lockbox detail for same date
        if not lb_detail.empty:
            lb_day = lb_detail[lb_detail['Processed Date'].dt.strftime('%Y-%m-%d') == dt]
            lb_detail_total = lb_day['Amount'].sum() if not lb_day.empty else 0
            lb_count = len(lb_day)
        else:
            lb_detail_total = 0
            lb_count = 0
        matched = abs(lb_bank_total - lb_detail_total) < 0.01
        match_class = 'match-yes' if matched else 'match-no'
        match_text = 'YES' if matched else f'NO — diff {fmt_money(abs(lb_bank_total - lb_detail_total))}'
        stats = f'Lockbox: <strong>{fmt_money(lb_bank_total)}</strong> vs Detail: <strong>{fmt_money(lb_detail_total)}</strong> ({lb_count} checks) &bull; <span class="{match_class}">{match_text}</span>'
        if chk_total > 0:
            stats += f' &bull; Check Deposits: <strong>{fmt_money(chk_total)}</strong>'
        html += f"""<tr class="date-header">
            <td colspan="5">
                <span class="date-label">{date_display}</span>
                <span class="date-stats">{stats}</span>
            </td>
        </tr>"""
        for _, r in day_lb.iterrows():
            sid = stable_id(f'bdep-{prefix}', dt, r['AMOUNT'], r['DESCRIPTION'][:40])
            html += f"""<tr data-row="{sid}">
            <td class="date-col">{date_display}</td>
            <td><span class="payer-badge lockbox-badge">Lockbox #11245</span></td>
            <td class="desc-col">{r['DESCRIPTION'][:60]}</td>
            <td class="amount">{fmt_money(r['AMOUNT'])}</td>
            <td class="posted-col">
                <select class="posted-select" data-row="{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                    <option value="partial">Partial</option>
                </select>
            </td>
        </tr>"""
        for _, r in day_chk.iterrows():
            sid = stable_id(f'bchk-{prefix}', dt, r['AMOUNT'], r['DESCRIPTION'][:40])
            html += f"""<tr data-row="{sid}">
            <td class="date-col">{date_display}</td>
            <td><span class="payer-badge deposit-badge">Check Deposit</span></td>
            <td class="desc-col">{r['DESCRIPTION'][:60]}</td>
            <td class="amount">{fmt_money(r['AMOUNT'])}</td>
            <td class="posted-col">
                <select class="posted-select" data-row="{sid}" onchange="updateStatus(this)">
                    <option value="">--</option>
                    <option value="yes">Yes</option>
                    <option value="no">No</option>
                    <option value="partial">Partial</option>
                </select>
            </td>
        </tr>"""
    return html

bank_dep_ppo_rows = build_bank_deposit_rows(bank_dep_ppo, lb_all, '11245', 'bppo')
total_bank_dep_ppo = bank_dep_ppo['AMOUNT'].sum() if not bank_dep_ppo.empty else 0

# Reconciliation badges
def match_badge(val_a, val_b):
    if abs(val_a - val_b) < 0.01:
        return '<span class="match-yes">MATCH</span>'
    else:
        return f'<span class="match-no">DIFF {fmt_money(abs(val_a - val_b))}</span>'

gs_lb_ppo = bg_ppo[bg_ppo['BG_CAT'] == 'Lockbox']['AMOUNT'].sum() if not bg_ppo.empty else 0
gs_dep_ppo = bg_ppo[bg_ppo['BG_CAT'] == 'Deposit']['AMOUNT'].sum() if not bg_ppo.empty else 0
match_lb = match_badge(total_lb_all, gs_lb_ppo)
match_dep_checks_badge = match_badge(total_dep_checks, gs_dep_ppo)

# Build all HTML rows
dep_rows = detail_deposit_rows(deposits)
eft_rows_html = detail_eft_rows(eft)
eft_med_rows_html = detail_eft_rows(eft_medicaid)
chk_dep_rows_html = detail_check_deposit_rows(check_dep)
cash_dep_rows_html = detail_cash_deposit_rows(cash_dep)
out_rows = detail_outgoing_rows(outgoing)

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Abra CT — Bank Posting — {date_min} to {date_max}</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: 'Poppins', sans-serif;
        background: #f0f4f8;
        color: #1a1a2e;
        padding: 0;
        font-size: 15px;
    }}

    .header {{
        background: linear-gradient(135deg, #0d4f3c 0%, #0f766e 50%, #14b8a6 100%);
        color: white;
        padding: 24px 36px;
        display: flex;
        align-items: center;
        gap: 20px;
        box-shadow: 0 4px 20px rgba(13, 79, 60, 0.3);
    }}
    .header img {{ height: 50px; }}
    .header h1 {{ font-size: 28px; font-weight: 800; }}
    .header .subtitle {{ font-size: 14px; opacity: 0.85; }}
    .header .date-range {{
        margin-left: auto;
        font-size: 18px;
        font-weight: 600;
        background: rgba(255,255,255,0.15);
        padding: 8px 18px;
        border-radius: 10px;
    }}

    .tab-bar {{
        background: #0d4f3c;
        padding: 0 36px;
        display: flex;
        gap: 0;
        box-shadow: 0 2px 8px rgba(0,0,0,0.15);
        flex-wrap: wrap;
    }}
    .tab-btn {{
        padding: 14px 22px;
        color: rgba(255,255,255,0.6);
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
        border: none;
        background: none;
        font-family: 'Poppins', sans-serif;
        border-bottom: 3px solid transparent;
        transition: all 0.2s;
    }}
    .tab-btn:hover {{ color: rgba(255,255,255,0.9); }}
    .tab-btn.active {{
        color: white;
        border-bottom-color: #14b8a6;
        background: rgba(255,255,255,0.08);
    }}
    .tab-btn .tab-count {{
        font-size: 11px;
        background: rgba(255,255,255,0.15);
        padding: 2px 8px;
        border-radius: 10px;
        margin-left: 6px;
    }}
    .tab-btn.active .tab-count {{ background: #14b8a6; }}

    .tab-content {{ display: none; padding: 24px 36px; }}
    .tab-content.active {{ display: block; }}

    .summary-row {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 14px;
        margin-bottom: 24px;
    }}
    .card {{
        background: white;
        border-radius: 14px;
        padding: 20px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.06);
        position: relative;
        overflow: hidden;
    }}
    .card::before {{
        content: '';
        position: absolute;
        top: 0; left: 0; right: 0;
        height: 5px;
    }}
    .card.c-deposit::before {{ background: linear-gradient(90deg, #00D2A0, #00B4D8); }}
    .card.c-eft::before {{ background: linear-gradient(90deg, #0f766e, #0d4f3c); }}
    .card.c-lockbox::before {{ background: linear-gradient(90deg, #A855F7, #7c3aed); }}
    .card.c-out::before {{ background: linear-gradient(90deg, #FF6B6B, #e05555); }}
    .card.c-net::before {{ background: linear-gradient(90deg, #FF9F43, #e08a2e); }}
    .card .card-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: #888; font-weight: 600; }}
    .card .card-value {{ font-size: 24px; font-weight: 800; margin: 6px 0 2px; }}
    .card.c-deposit .card-value {{ color: #00a882; }}
    .card.c-eft .card-value {{ color: #0f766e; }}
    .card.c-lockbox .card-value {{ color: #A855F7; }}
    .card.c-out .card-value {{ color: #FF6B6B; }}
    .card.c-net .card-value {{ color: #FF9F43; }}
    .card .card-count {{ font-size: 12px; color: #aaa; }}

    .howto {{
        background: white;
        border-radius: 12px;
        padding: 20px 24px;
        margin-bottom: 24px;
        border-left: 5px solid #14b8a6;
        box-shadow: 0 2px 8px rgba(0,0,0,0.05);
    }}
    .howto h3 {{ font-size: 17px; color: #0d4f3c; margin-bottom: 10px; }}
    .howto ul {{ list-style: none; padding: 0; }}
    .howto li {{ padding: 5px 0; font-size: 14px; display: flex; align-items: center; gap: 10px; }}
    .step-icon {{
        display: inline-flex; align-items: center; justify-content: center;
        width: 26px; height: 26px; border-radius: 50%;
        background: #0d4f3c; color: white;
        font-size: 12px; font-weight: 700; flex-shrink: 0;
    }}

    .overview-table {{
        width: 100%;
        border-collapse: collapse;
        background: white;
        border-radius: 12px;
        overflow: hidden;
        box-shadow: 0 2px 10px rgba(0,0,0,0.06);
    }}
    .overview-table th {{
        padding: 14px 20px;
        text-align: left;
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        color: #666;
        background: #f8f9fb;
        border-bottom: 2px solid #e8ecf0;
    }}
    .overview-table td {{
        padding: 12px 20px;
        font-size: 15px;
        border-bottom: 1px solid #f0f0f0;
    }}
    .overview-table tr:hover td {{ background: #fafcff; }}

    .cat-badge {{
        display: inline-block;
        padding: 5px 16px;
        border-radius: 20px;
        font-size: 13px;
        font-weight: 700;
    }}
    .cat-deposits {{ background: #e6fbf5; color: #00875a; }}
    .cat-eft {{ background: #e6fff9; color: #0d4f3c; }}
    .cat-lockbox {{ background: #f3e8ff; color: #7c3aed; }}
    .cat-outgoing {{ background: #ffe8e8; color: #cc3333; }}
    .cat-checkdeposits {{ background: #fff3e0; color: #e65100; }}
    .cat-otherdeposits {{ background: #e8f5e9; color: #2e7d32; }}

    .count-col {{ text-align: center; font-weight: 600; }}

    .detail-block {{
        background: white;
        border-radius: 14px;
        margin-bottom: 20px;
        box-shadow: 0 2px 12px rgba(0,0,0,0.06);
        overflow: hidden;
    }}
    .detail-header {{
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 18px 24px;
        color: white;
    }}
    .dh-deposit {{ background: linear-gradient(135deg, #00D2A0, #00B4D8); }}
    .dh-eft {{ background: linear-gradient(135deg, #0f766e, #0d4f3c); }}
    .dh-lockbox {{ background: linear-gradient(135deg, #A855F7, #7c3aed); }}
    .dh-medicaid {{ background: linear-gradient(135deg, #7c3aed, #5b21b6); }}
    .dh-outgoing {{ background: linear-gradient(135deg, #FF6B6B, #d44); }}
    .dh-checkdep {{ background: linear-gradient(135deg, #FF9F43, #e08a2e); }}
    .dh-other {{ background: linear-gradient(135deg, #2e7d32, #4caf50); }}
    .detail-icon {{ font-size: 26px; }}
    .detail-title {{ font-size: 20px; font-weight: 700; }}
    .detail-sub {{ font-size: 13px; opacity: 0.85; margin-top: 2px; }}
    .detail-posted {{
        font-size: 16px;
        font-weight: 700;
        color: #00D2A0;
        background: rgba(0,210,160,0.15);
        padding: 6px 14px;
        border-radius: 10px;
        margin-left: 8px;
        white-space: nowrap;
    }}
    .download-btn {{
        background: rgba(255,255,255,0.2);
        color: white;
        border: 1px solid rgba(255,255,255,0.3);
        padding: 6px 14px;
        border-radius: 8px;
        font-family: 'Poppins', sans-serif;
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
        margin-left: 10px;
        transition: background 0.2s;
    }}
    .download-btn:hover {{ background: rgba(255,255,255,0.35); }}
    .detail-total {{
        margin-left: auto;
        font-size: 22px;
        font-weight: 800;
        background: rgba(255,255,255,0.2);
        padding: 8px 18px;
        border-radius: 10px;
    }}

    table {{ width: 100%; border-collapse: collapse; }}
    th {{
        padding: 12px 16px;
        text-align: left;
        font-size: 11px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.8px;
        color: #777;
        background: #f8f9fb;
        border-bottom: 2px solid #e8ecf0;
    }}
    td {{
        padding: 10px 16px;
        font-size: 14px;
        border-bottom: 1px solid #f2f2f2;
        vertical-align: middle;
    }}
    tr:hover td {{ background: #fafcff; }}
    .date-header td {{
        background: #f0fdf8 !important;
        padding: 13px 16px;
        border-bottom: 2px solid #a7f3d0;
    }}
    .date-label {{ font-weight: 800; font-size: 16px; color: #0d4f3c; }}
    .date-stats {{ float: right; font-size: 14px; color: #555; font-weight: 500; }}
    .amount {{ text-align: right; font-weight: 700; font-size: 15px; font-variant-numeric: tabular-nums; color: #1a7a5a; }}
    .negative {{ color: #FF6B6B !important; }}
    .date-col {{ color: #888; font-size: 13px; }}
    .ach-col {{ font-family: 'Courier New', monospace; font-size: 12px; color: #666; }}
    .desc-col {{ font-size: 13px; color: #888; }}
    .type-col {{ font-size: 12px; color: #666; }}
    .check-col {{ font-family: 'Courier New', monospace; font-size: 13px; font-weight: 600; color: #333; }}

    .payer-badge {{
        display: inline-block;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 13px;
        font-weight: 600;
    }}
    .deposit-badge {{ background: #e6fbf5; color: #00875a; }}
    .eft-badge {{ background: #e6fff9; color: #0d4f3c; }}
    .lockbox-badge {{ background: #f3e8ff; color: #7c3aed; }}
    .outgoing-badge {{ background: #ffe8e8; color: #cc3333; }}
    .cash-badge {{ background: #fff3e0; color: #e65100; }}
    .other-badge {{ background: #e8f5e9; color: #2e7d32; }}

    .posted-col {{ text-align: center; }}
    .posted-select {{
        font-family: 'Poppins', sans-serif;
        font-size: 13px;
        font-weight: 600;
        padding: 6px 12px;
        border-radius: 8px;
        border: 2px solid #ddd;
        cursor: pointer;
        background: white;
        min-width: 90px;
        transition: all 0.2s;
    }}
    .posted-select:focus {{ outline: none; border-color: #0f766e; }}
    .posted-select.status-yes {{ background: #e6fbf5; border-color: #00D2A0; color: #00875a; }}
    .posted-select.status-no {{ background: #ffe8e8; border-color: #FF6B6B; color: #cc3333; }}
    .posted-select.status-partial {{ background: #fff8e6; border-color: #FF9F43; color: #c77a20; }}

    .remarks-col {{ padding: 6px 8px !important; }}
    .remarks-input {{
        font-family: 'Poppins', sans-serif;
        font-size: 12px;
        padding: 6px 10px;
        border: 2px solid #ddd;
        border-radius: 8px;
        width: 100%;
        min-width: 140px;
        transition: border-color 0.2s;
        background: white;
    }}
    .remarks-input:focus {{ outline: none; border-color: #0f766e; background: #f0fdf8; }}
    .remarks-input.has-text {{ border-color: #14b8a6; background: #f0fdf8; }}

    tr.row-yes td {{ background: #f0fdf4 !important; }}
    tr.row-no td {{ background: #fff5f5 !important; }}
    tr.row-partial td {{ background: #fffbeb !important; }}

    .progress-bar {{
        background: white;
        border-radius: 12px;
        padding: 16px 24px;
        margin-bottom: 20px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        display: flex;
        align-items: center;
        gap: 16px;
    }}
    .progress-bar .prog-label {{ font-size: 14px; font-weight: 600; color: #555; white-space: nowrap; }}
    .prog-track {{
        flex: 1;
        height: 12px;
        background: #e8ecf0;
        border-radius: 6px;
        overflow: hidden;
    }}
    .prog-fill {{
        height: 100%;
        background: linear-gradient(90deg, #00D2A0, #00B4D8);
        border-radius: 6px;
        transition: width 0.4s;
        width: 0%;
    }}
    .prog-text {{ font-size: 14px; font-weight: 700; color: #0d4f3c; min-width: 50px; text-align: right; }}

    .dual-acct {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 20px;
        margin-bottom: 24px;
    }}
    .acct-block {{
        background: white;
        border-radius: 14px;
        overflow: hidden;
        box-shadow: 0 2px 12px rgba(0,0,0,0.06);
    }}
    .acct-header {{
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 18px 24px;
        color: white;
    }}
    .ppo-header {{ background: linear-gradient(135deg, #0f766e, #0d4f3c); }}
    .med-header {{ background: linear-gradient(135deg, #7c3aed, #5b21b6); }}
    .acct-icon {{ font-size: 26px; }}
    .acct-title {{ font-size: 20px; font-weight: 700; }}
    .acct-num {{ font-size: 13px; opacity: 0.8; }}
    .acct-total {{
        margin-left: auto;
        font-size: 22px;
        font-weight: 800;
        background: rgba(255,255,255,0.2);
        padding: 8px 18px;
        border-radius: 10px;
    }}
    .total-row td {{
        border-top: 3px solid #0d4f3c;
        background: #f0fdf8 !important;
        font-size: 16px;
    }}
    .match-col {{ text-align: center; }}
    .match-yes {{ background: #e6fbf5; color: #00875a; padding: 4px 14px; border-radius: 20px; font-weight: 700; font-size: 13px; }}
    .match-no {{ background: #ffe8e8; color: #cc3333; padding: 4px 14px; border-radius: 20px; font-weight: 700; font-size: 13px; }}

    .recon-block {{
        background: white;
        border-radius: 14px;
        overflow: hidden;
        box-shadow: 0 2px 12px rgba(0,0,0,0.06);
    }}
    .recon-header {{
        background: linear-gradient(135deg, #FF9F43, #e08a2e);
        color: white;
        padding: 16px 24px;
        font-size: 18px;
        font-weight: 700;
    }}

    .month-select {{
        font-family: 'Poppins', sans-serif;
        font-size: 15px;
        font-weight: 600;
        padding: 8px 16px;
        border-radius: 10px;
        border: 2px solid rgba(255,255,255,0.3);
        background: rgba(255,255,255,0.15);
        color: white;
        cursor: pointer;
        margin-left: 12px;
    }}
    .month-select option {{
        background: #0d4f3c;
        color: white;
    }}

    @media print {{
        body {{ font-size: 11px; }}
        .tab-bar {{ display: none; }}
        .tab-content {{ display: block !important; padding: 12px; }}
        .posted-select {{ border: 1px solid #ccc; }}
    }}
</style>
<script src="https://www.gstatic.com/firebasejs/9.23.0/firebase-app-compat.js"></script>
<script src="https://www.gstatic.com/firebasejs/9.23.0/firebase-database-compat.js"></script>
</head>
<body>

<div class="header">
    <img src="https://abrahealthgroup.com/wp-content/uploads/2022/09/Asset-48@4x-copy.png" alt="Abra Health">
    <div>
        <h1>Abra CT — Bank Posting</h1>
        <div class="subtitle">What needs to be posted in Open Dental</div>
    </div>
    <select class="month-select" id="monthSelect" onchange="switchMonth(this.value)">
        <option value="{MONTH_KEY}">{MONTH_LABEL}</option>
    </select>
    <div class="date-range">{date_min} - {date_max}</div>
</div>

<div class="tab-bar">
    <button class="tab-btn active" onclick="showTab('overview')">Overview</button>
    <button class="tab-btn" onclick="showTab('bankdep')">Deposits</button>
    <button class="tab-btn" onclick="showTab('eft')">PPO EFT <span class="tab-count">{len(eft)}</span></button>
    <button class="tab-btn" onclick="showTab('eftmed')">Medicaid EFT <span class="tab-count">{len(eft_medicaid)}</span></button>
    <button class="tab-btn" onclick="showTab('lockbox')">Lockbox <span class="tab-count">{len(lb_all)}</span></button>
    <button class="tab-btn" onclick="showTab('depchk')">Deposited Checks <span class="tab-count">{num_dep_checks}</span></button>
    <button class="tab-btn" onclick="showTab('cashdep')">Cash Deposits <span class="tab-count">{len(cash_dep)}</span></button>
    <button class="tab-btn" onclick="showTab('outgoing')">Outgoing <span class="tab-count">{len(outgoing)}</span></button>
    <button class="tab-btn" onclick="showTab('carddeposits')">Card Deposits <span class="tab-count">{len(deposits)}</span></button>
</div>

<!-- ==================== OVERVIEW TAB ==================== -->
<div id="tab-overview" class="tab-content active">

    <div class="howto">
        <h3>How to Use This Report</h3>
        <ul>
            <li><span class="step-icon">1</span> <strong>CARD DEPOSITS</strong> (green) = Credit card batches by location (Danbury, Derby, Norwalk, Stamford, Bridgeport). Match to CC batch reports.</li>
            <li><span class="step-icon">2</span> <strong>PPO EFT</strong> (blue) = Cigna and other PPO insurance payments. Match to ERA/835 files.</li>
            <li><span class="step-icon">3</span> <strong>MEDICAID EFT</strong> (purple) = CT Medicaid (DSS) payments. Match to ERA/835 files.</li>
            <li><span class="step-icon">4</span> <strong>CASH DEPOSITS</strong> (orange) = Curr/Coin deposits at the bank. Match to daily cash reports.</li>
            <li><span class="step-icon">5</span> <strong>LOCKBOX</strong> (purple) = Lockbox #11245 check detail. Use to match individual checks.</li>
            <li><span class="step-icon">!</span> <strong>OUTGOING</strong> (red) = All debits (payroll, utilities, fees). Review for accuracy.</li>
        </ul>
    </div>

    <div class="progress-bar">
        <div class="prog-label">PPO Posting:</div>
        <div class="prog-track"><div class="prog-fill" id="progressFillPPO" style="background:linear-gradient(90deg,#0f766e,#14b8a6)"></div></div>
        <div class="prog-text" id="progressTextPPO">0%</div>
    </div>
    <div class="progress-bar" style="margin-top:8px">
        <div class="prog-label">Medicaid Posting:</div>
        <div class="prog-track"><div class="prog-fill" id="progressFillMed" style="background:linear-gradient(90deg,#A855F7,#7c3aed)"></div></div>
        <div class="prog-text" id="progressTextMed">0%</div>
    </div>

    <!-- DUAL ACCOUNT OVERVIEW -->
    <div class="dual-acct">
        <div class="acct-block">
            <div class="acct-header ppo-header">
                <div class="acct-icon">🏦</div>
                <div>
                    <div class="acct-title">PPO Account</div>
                    <div class="acct-num">Presto Den ...1622</div>
                </div>
                <div class="acct-total">{fmt_money(ppo_total)}</div>
            </div>
            <table class="overview-table">
                <thead><tr>
                    <th>Date</th>
                    <th style="text-align:right">EFT</th>
                    <th style="text-align:right">Lockbox</th>
                    <th style="text-align:right">Dep. Checks</th>
                    <th style="text-align:right">Day Total</th>
                </tr></thead>
                <tbody>{ppo_overview_rows if ppo_overview_rows else '<tr><td colspan="5" style="text-align:center;color:#888;padding:20px">No Bank General Statement loaded for PPO</td></tr>'}</tbody>
            </table>
        </div>
        <div class="acct-block">
            <div class="acct-header med-header">
                <div class="acct-icon">🏦</div>
                <div>
                    <div class="acct-title">Medicaid Account</div>
                    <div class="acct-num">PrestoDenZBA ...1630</div>
                </div>
                <div class="acct-total">{fmt_money(med_total)}</div>
            </div>
            <table class="overview-table">
                <thead><tr>
                    <th>Date</th>
                    <th style="text-align:center">EFTs</th>
                    <th style="text-align:right">Amount</th>
                </tr></thead>
                <tbody>{med_overview_rows if med_overview_rows else '<tr><td colspan="3" style="text-align:center;color:#888;padding:20px">No Medicaid EFT data</td></tr>'}</tbody>
            </table>
        </div>
    </div>

    <!-- EFT SUMMARY -->
    <div class="recon-block">
        <div class="recon-header">EFT Summary — Report Builder</div>
        <table class="overview-table">
            <thead><tr>
                <th>Category</th>
                <th style="text-align:center">Count</th>
                <th style="text-align:right">Total Amount</th>
            </tr></thead>
            <tbody>
                <tr>
                    <td><span class="cat-badge cat-eft">PPO EFT</span></td>
                    <td class="count-col">{len(eft)}</td>
                    <td class="amount">{fmt_money(total_eft)}</td>
                </tr>
                <tr>
                    <td><span class="cat-badge cat-eft">Medicaid EFT</span></td>
                    <td class="count-col">{len(eft_medicaid)}</td>
                    <td class="amount">{fmt_money(total_eft_medicaid)}</td>
                </tr>
                <tr style="border-top:2px solid #0d4f3c">
                    <td><strong>Total EFT</strong></td>
                    <td class="count-col"><strong>{len(eft) + len(eft_medicaid)}</strong></td>
                    <td class="amount"><strong>{fmt_money(total_eft + total_eft_medicaid)}</strong></td>
                </tr>
            </tbody>
        </table>
    </div>

    <!-- LOCKBOX RECONCILIATION -->
    <div class="recon-block" style="margin-top:16px">
        <div class="recon-header">Lockbox Reconciliation — LockBox Folder vs General Statement</div>
        <table class="overview-table">
            <thead><tr>
                <th>Category</th>
                <th style="text-align:center">Count</th>
                <th style="text-align:right">LockBox Folder</th>
                <th style="text-align:right">General Statement</th>
                <th style="text-align:center">Status</th>
            </tr></thead>
            <tbody>
                <tr>
                    <td><span class="cat-badge cat-lockbox">Lockbox #11245</span></td>
                    <td class="count-col">{len(lb_all)}</td>
                    <td class="amount">{fmt_money(total_lb_all)}</td>
                    <td class="amount">{fmt_money(gs_lb_ppo)}</td>
                    <td class="match-col">{match_lb}</td>
                </tr>
            </tbody>
        </table>
    </div>

    <!-- DEPOSITED CHECKS RECONCILIATION -->
    <div class="recon-block" style="margin-top:16px">
        <div class="recon-header">Deposited Checks Reconciliation — Checks Folder vs General Statement</div>
        <table class="overview-table">
            <thead><tr>
                <th>Category</th>
                <th style="text-align:center">Count</th>
                <th style="text-align:right">Checks Folder</th>
                <th style="text-align:right">General Statement</th>
                <th style="text-align:center">Status</th>
            </tr></thead>
            <tbody>
                <tr>
                    <td><span class="cat-badge cat-deposits">Deposited Checks</span></td>
                    <td class="count-col">{num_dep_checks}</td>
                    <td class="amount">{fmt_money(total_dep_checks)}</td>
                    <td class="amount">{fmt_money(gs_dep_ppo)}</td>
                    <td class="match-col">{match_dep_checks_badge}</td>
                </tr>
            </tbody>
        </table>
    </div>
</div>

<!-- ==================== BANK DEPOSITS TAB ==================== -->
<div id="tab-bankdep" class="tab-content">
    <div class="detail-block">
        <div class="detail-header ppo-header">
            <div class="detail-icon">🏦</div>
            <div>
                <div class="detail-title">PPO Deposits — Bank Statement vs Lockbox #11245</div>
                <div class="detail-sub">Each date shows bank deposit total vs lockbox detail total</div>
            </div>
            <div class="detail-total">{fmt_money(total_bank_dep_ppo)}</div>
            <button class="download-btn" onclick="downloadTab('tab-bankdep','Deposits')">CSV</button>
        </div>
        <table>
            <thead><tr>
                <th style="width:90px">Date</th>
                <th>Type</th>
                <th>Description</th>
                <th style="width:120px">Amount</th>
                <th style="width:100px">Reconciled</th>
            </tr></thead>
            <tbody>{bank_dep_ppo_rows if bank_dep_ppo_rows else '<tr><td colspan="5" style="text-align:center;color:#888;padding:20px">No Bank General Statement data available</td></tr>'}</tbody>
        </table>
    </div>
</div>

<!-- ==================== CARD DEPOSITS TAB ==================== -->
<div id="tab-carddeposits" class="tab-content">
    <div class="detail-block">
        <div class="detail-header dh-deposit">
            <div class="detail-icon">💳</div>
            <div>
                <div class="detail-title">CARD DEPOSITS — Merchant & Global Payments</div>
                <div class="detail-sub">Match these to credit card batch reports in Open Dental</div>
            </div>
            <div class="detail-total">{fmt_money(total_deposits)}</div>
            <button class="download-btn" onclick="downloadTab('tab-carddeposits','Card_Deposits')">CSV</button>
        </div>
        <table>
            <thead><tr>
                <th style="width:90px">Date</th>
                <th>Location / Source</th>
                <th style="width:80px">Type</th>
                <th style="width:110px">Amount</th>
                <th>ACH Individual ID</th>
            </tr></thead>
            <tbody>{dep_rows}</tbody>
        </table>
    </div>
</div>

<!-- ==================== PPO EFT TAB ==================== -->
<div id="tab-eft" class="tab-content">
    <div class="detail-block">
        <div class="detail-header dh-eft">
            <div class="detail-icon">🏥</div>
            <div>
                <div class="detail-title">PPO EFT — Insurance Electronic Payments</div>
                <div class="detail-sub">Match to ERA/835 files and post insurance payments in Open Dental</div>
            </div>
            <div class="detail-total">{fmt_money(total_eft)}</div>
            <div class="detail-posted" id="posted-tab-eft">Posted: $0.00</div>
            <button class="download-btn" onclick="downloadTab('tab-eft','PPO_EFT')">CSV</button>
        </div>
        <table>
            <thead><tr>
                <th style="width:90px">Date</th>
                <th>Insurance Payer</th>
                <th style="width:110px">Amount</th>
                <th>ACH Individual ID</th>
                <th>Payer Name</th>
                <th style="width:120px">EOB Downloaded</th>
                <th style="width:100px">OD Posted</th>
                <th style="width:180px">Remarks</th>
            </tr></thead>
            <tbody>{eft_rows_html}</tbody>
        </table>
    </div>
</div>

<!-- ==================== MEDICAID EFT TAB ==================== -->
<div id="tab-eftmed" class="tab-content">
    <div class="detail-block">
        <div class="detail-header dh-medicaid">
            <div class="detail-icon">🏥</div>
            <div>
                <div class="detail-title">Medicaid EFT — CT DSS Payments</div>
                <div class="detail-sub">Match to ERA/835 files and post insurance payments in Open Dental</div>
            </div>
            <div class="detail-total">{fmt_money(total_eft_medicaid)}</div>
            <div class="detail-posted" id="posted-tab-eftmed">Posted: $0.00</div>
            <button class="download-btn" onclick="downloadTab('tab-eftmed','Medicaid_EFT')">CSV</button>
        </div>
        <table>
            <thead><tr>
                <th style="width:90px">Date</th>
                <th>Insurance Payer</th>
                <th style="width:110px">Amount</th>
                <th>ACH Individual ID</th>
                <th>Payer Name</th>
                <th style="width:120px">EOB Downloaded</th>
                <th style="width:100px">OD Posted</th>
                <th style="width:180px">Remarks</th>
            </tr></thead>
            <tbody>{eft_med_rows_html}</tbody>
        </table>
    </div>
</div>

<!-- ==================== LOCKBOX TAB ==================== -->
<div id="tab-lockbox" class="tab-content">
    <div class="detail-block">
        <div class="detail-header dh-lockbox">
            <div class="detail-icon">📬</div>
            <div>
                <div class="detail-title">LOCKBOX — #11245</div>
                <div class="detail-sub">Insurance checks — match to EOBs and post in Open Dental</div>
            </div>
            <div class="detail-total">{fmt_money(total_lb_all)}</div>
            <div class="detail-posted" id="posted-tab-lockbox">Posted: $0.00</div>
            <button class="download-btn" onclick="downloadTab('tab-lockbox','Lockbox')">CSV</button>
        </div>
        <table>
            <thead><tr>
                <th style="width:90px">Date</th>
                <th>Check Number</th>
                <th style="width:110px">Amount</th>
                <th style="width:120px">EOB Downloaded</th>
                <th style="width:100px">OD Posted</th>
                <th style="width:180px">Remarks</th>
            </tr></thead>
            <tbody>{lb_all_rows if lb_all_rows else '<tr><td colspan="6" style="text-align:center;color:#888;padding:20px">No lockbox CSV files loaded yet</td></tr>'}</tbody>
        </table>
    </div>
</div>

<!-- ==================== CASH DEPOSITS TAB ==================== -->
<div id="tab-cashdep" class="tab-content">
    <div class="detail-block">
        <div class="detail-header dh-checkdep">
            <div class="detail-icon">💵</div>
            <div>
                <div class="detail-title">CASH DEPOSITS — Curr/Coin Deposits</div>
                <div class="detail-sub">Cash deposits at the bank — match to daily cash reports</div>
            </div>
            <div class="detail-total">{fmt_money(total_cash_dep)}</div>
            <div class="detail-posted" id="posted-tab-cashdep">Posted: $0.00</div>
            <button class="download-btn" onclick="downloadTab('tab-cashdep','Cash_Deposits')">CSV</button>
        </div>
        <table>
            <thead><tr>
                <th style="width:90px">Date</th>
                <th>Type</th>
                <th style="width:110px">Amount</th>
                <th>Description</th>
                <th style="width:100px">OD Posted</th>
                <th style="width:180px">Remarks</th>
            </tr></thead>
            <tbody>{cash_dep_rows_html if cash_dep_rows_html else '<tr><td colspan="6" style="text-align:center;color:#888;padding:20px">No cash deposits in this period</td></tr>'}</tbody>
        </table>
    </div>
</div>

<!-- ==================== DEPOSITED CHECKS TAB ==================== -->
<div id="tab-depchk" class="tab-content">
    <div class="detail-block">
        <div class="detail-header dh-deposit">
            <div class="detail-icon">🏦</div>
            <div>
                <div class="detail-title">DEPOSITED CHECKS — Individual Check Breakdown</div>
                <div class="detail-sub">Checks deposited at the bank — match to General Deposit line items</div>
            </div>
            <div class="detail-total">{fmt_money(total_dep_checks)}</div>
            <div class="detail-posted" id="posted-tab-depchk">Posted: $0.00</div>
            <button class="download-btn" onclick="downloadTab('tab-depchk','Deposited_Checks')">CSV</button>
        </div>
        <table>
            <thead><tr>
                <th style="width:90px">Date</th>
                <th>Check #</th>
                <th style="width:110px">Amount</th>
                <th>From Account</th>
                <th>Routing #</th>
                <th style="width:80px">EOB</th>
                <th style="width:100px">OD Posted</th>
                <th style="width:180px">Remarks</th>
            </tr></thead>
            <tbody>{dep_check_rows_html if dep_check_rows_html else '<tr><td colspan="8" style="text-align:center;color:#888;padding:20px">No deposited check files loaded yet</td></tr>'}</tbody>
        </table>
    </div>
</div>

<!-- ==================== OUTGOING TAB ==================== -->
<div id="tab-outgoing" class="tab-content">
    <div class="detail-block">
        <div class="detail-header dh-outgoing">
            <div class="detail-icon">📤</div>
            <div>
                <div class="detail-title">OUTGOING — Debits & Payments</div>
                <div class="detail-sub">All outgoing transactions — payroll, utilities, fees, etc.</div>
            </div>
            <div class="detail-total">({fmt_money(abs(total_outgoing_all))})</div>
            <button class="download-btn" onclick="downloadTab('tab-outgoing','Outgoing')">CSV</button>
        </div>
        <table>
            <thead><tr>
                <th style="width:90px">Date</th>
                <th>To Account</th>
                <th style="width:110px">Amount</th>
                <th>ACH Individual ID</th>
                <th>Description</th>
                <th style="width:200px">Remarks</th>
            </tr></thead>
            <tbody>{out_rows}</tbody>
        </table>
    </div>
</div>

<script>
// === Firebase Setup — CT Posting (dedicated project) ===
const firebaseConfig = {{
    apiKey: "AIzaSyBvMOVV9l-cHl_m7glJ_EwbS4occJGchyo",
    authDomain: "ct-posting-abra.firebaseapp.com",
    databaseURL: "https://ct-posting-abra-default-rtdb.firebaseio.com",
    projectId: "ct-posting-abra",
    storageBucket: "ct-posting-abra.firebasestorage.app",
    messagingSenderId: "940312009238",
    appId: "1:940312009238:web:9ff2dc6c4f700fe791f337"
}};
firebase.initializeApp(firebaseConfig);
const db = firebase.database();
// Namespaced by month so each month's posting status is independent
const MONTH_KEY = '{MONTH_KEY}';
const statusRef = db.ref('ct_statuses/' + MONTH_KEY);
const remarksRef = db.ref('ct_remarks/' + MONTH_KEY);

function showTab(name) {{
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    event.target.closest('.tab-btn').classList.add('active');
}}

function applyStatusToElement(select, val) {{
    const row = select.closest('tr');
    select.classList.remove('status-yes', 'status-no', 'status-partial');
    if (row) row.classList.remove('row-yes', 'row-no', 'row-partial');
    if (val) {{
        select.value = val;
        select.classList.add('status-' + val);
        if (row) row.classList.add('row-' + val);
    }} else {{
        select.value = '';
    }}
}}

function updateStatus(select) {{
    const val = select.value;
    applyStatusToElement(select, val);
    const rowId = select.dataset.row;
    statusRef.child(rowId).set(val || null);
    updateProgress();
}}

function saveRemark(input) {{
    const rowId = input.dataset.row;
    const val = input.value;
    remarksRef.child(rowId).set(val || null);
    if (val.trim()) {{
        input.classList.add('has-text');
    }} else {{
        input.classList.remove('has-text');
    }}
}}

function getRowAmount(select) {{
    const row = select.closest('tr');
    if (!row) return 0;
    const amtCell = row.querySelector('.amount');
    if (!amtCell) return 0;
    const txt = amtCell.textContent.replace(/[$,()]/g, '').trim();
    const val = parseFloat(txt);
    return isNaN(val) ? 0 : val;
}}

function updateProgress() {{
    const ppoTabs = ['tab-eft', 'tab-lockbox', 'tab-depchk', 'tab-cashdep'];
    const medTabs = ['tab-eftmed'];

    // Dollar-based progress: yes = 100%, partial = 50%
    function countProgress(tabIds) {{
        let totalAmt = 0, postedAmt = 0;
        tabIds.forEach(id => {{
            const tab = document.getElementById(id);
            if (tab) {{
                const selects = tab.querySelectorAll('.posted-select:not(.eob-select)');
                selects.forEach(s => {{
                    const amt = getRowAmount(s);
                    totalAmt += amt;
                    if (s.value === 'yes') postedAmt += amt;
                    else if (s.value === 'partial') postedAmt += amt * 0.5;
                }});
            }}
        }});
        return totalAmt > 0 ? Math.round((postedAmt / totalAmt) * 100) : 0;
    }}

    const ppoPct = countProgress(ppoTabs);
    document.getElementById('progressFillPPO').style.width = ppoPct + '%';
    document.getElementById('progressTextPPO').textContent = ppoPct + '%';

    const medPct = countProgress(medTabs);
    document.getElementById('progressFillMed').style.width = medPct + '%';
    document.getElementById('progressTextMed').textContent = medPct + '%';

    // Per-tab posted totals
    const tabsWithPosted = ['tab-eft', 'tab-eftmed', 'tab-lockbox', 'tab-depchk', 'tab-cashdep'];
    tabsWithPosted.forEach(tabId => {{
        const tab = document.getElementById(tabId);
        const el = document.getElementById('posted-' + tabId);
        if (!tab || !el) return;
        let postedAmt = 0;
        const selects = tab.querySelectorAll('.posted-select:not(.eob-select)');
        selects.forEach(s => {{
            if (s.value === 'yes') {{
                postedAmt += getRowAmount(s);
            }} else if (s.value === 'partial') {{
                postedAmt += getRowAmount(s) * 0.5;
            }}
        }});
        el.textContent = 'Posted: $' + postedAmt.toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
    }});
}}

function downloadTab(tabId, filename) {{
    const tab = document.getElementById(tabId);
    if (!tab) return;
    const table = tab.querySelector('table');
    if (!table) return;
    let csv = [];
    const rows = table.querySelectorAll('tr');
    rows.forEach(row => {{
        if (row.classList.contains('date-header')) return;
        const cells = [];
        row.querySelectorAll('th, td').forEach(cell => {{
            let text = '';
            const select = cell.querySelector('select');
            const input = cell.querySelector('input');
            if (select) {{
                text = select.value || '';
            }} else if (input) {{
                text = input.value || '';
            }} else {{
                text = cell.textContent.trim();
            }}
            text = text.replace(/"/g, '""');
            cells.push('"' + text + '"');
        }});
        if (cells.length > 0) csv.push(cells.join(','));
    }});
    const blob = new Blob([csv.join('\\n')], {{type: 'text/csv'}});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename + '.csv';
    a.click();
    URL.revokeObjectURL(url);
}}

// === Month switching ===
function switchMonth(key) {{
    if (key === MONTH_KEY) return;
    window.location.href = key + '.html';
}}

// Load available months from months.json and populate dropdown
fetch('months.json')
    .then(r => r.json())
    .then(months => {{
        const sel = document.getElementById('monthSelect');
        sel.innerHTML = '';
        // months is sorted newest first: [{{"key":"2026-04","label":"April 2026"}}, ...]
        months.forEach(m => {{
            const opt = document.createElement('option');
            opt.value = m.key;
            opt.textContent = m.label;
            if (m.key === MONTH_KEY) opt.selected = true;
            sel.appendChild(opt);
        }});
    }})
    .catch(() => {{}});  // If months.json not found, just show current month

// === Real-time listeners ===
window.addEventListener('DOMContentLoaded', function() {{
    statusRef.on('value', function(snapshot) {{
        const data = snapshot.val() || {{}};
        document.querySelectorAll('.posted-select').forEach(select => {{
            const rowId = select.dataset.row;
            const val = data[rowId] || '';
            applyStatusToElement(select, val);
        }});
        updateProgress();
    }});

    remarksRef.on('value', function(snapshot) {{
        const data = snapshot.val() || {{}};
        document.querySelectorAll('.remarks-input').forEach(input => {{
            const rowId = input.dataset.row;
            const val = data[rowId] || '';
            input.value = val;
            if (val.trim()) {{
                input.classList.add('has-text');
            }} else {{
                input.classList.remove('has-text');
            }}
        }});
    }});
}});
</script>

</body>
</html>"""

# Save as month-specific file (e.g., 2026-04.html)
month_path = os.path.join(OUTPUT_DIR, f"{MONTH_KEY}.html")
with open(month_path, 'w') as f:
    f.write(html)

# Also save as dashboard.html (current/default view for GitHub Pages)
dashboard_path = os.path.join(OUTPUT_DIR, "dashboard.html")
with open(dashboard_path, 'w') as f:
    f.write(html)

# Update months.json — add current month if not present, keep sorted newest first
months_path = os.path.join(OUTPUT_DIR, "months.json")
if os.path.exists(months_path):
    with open(months_path, 'r') as f:
        months = json.load(f)
else:
    months = []

# Check if this month already exists
existing_keys = [m['key'] for m in months]
if MONTH_KEY not in existing_keys:
    months.append({"key": MONTH_KEY, "label": MONTH_LABEL})

# Sort newest first
months.sort(key=lambda m: m['key'], reverse=True)

with open(months_path, 'w') as f:
    json.dump(months, f, indent=2)

print(f"\nSaved to: {month_path}")
print(f"Also saved: {dashboard_path}")
print(f"Months available: {', '.join(m['label'] for m in months)}")
print(f"\nSummary:")
print(f"  Card Deposits:      {len(deposits)} txns = {fmt_money(total_deposits)}")
print(f"  PPO EFT:            {len(eft)} txns = {fmt_money(total_eft)}")
print(f"  Medicaid EFT:       {len(eft_medicaid)} txns = {fmt_money(total_eft_medicaid)}")
print(f"  Check Deposits:     {len(check_dep)} txns = {fmt_money(total_check_dep)}")
print(f"  Cash Deposits:      {len(cash_dep)} txns = {fmt_money(total_cash_dep)}")
print(f"  Lockbox:            {len(lb_all)} checks = {fmt_money(total_lb_all)}")
print(f"  Deposited Checks:   {num_dep_checks} checks = {fmt_money(total_dep_checks)}")
print(f"  Outgoing/Debits:    {len(outgoing)} txns = ({fmt_money(abs(total_outgoing_all))})")
print(f"  Net Activity:       {fmt_money(net_total)}")
