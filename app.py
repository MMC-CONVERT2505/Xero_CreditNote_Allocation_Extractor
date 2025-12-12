import os
import json
import requests
import pandas as pd
from io import BytesIO
from datetime import datetime
from flask import (
    Flask, request, session, jsonify, send_file,
    render_template, redirect, url_for
)
from dotenv import load_dotenv

# LOAD ENV
load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = "http://localhost:5000/callback"
BASE_URL = "https://api.xero.com/api.xro/2.0"
TOKEN_URL = "https://identity.xero.com/connect/token"
TOKEN_FILE = "xero_tokens.json"


# FLASK APP
app = Flask(__name__)
app.secret_key = "fixed_secret_key_2024"

# ============================================
# TOKEN HELPERS (your existing helpers, slightly reused)
# ============================================

def save_token(token_data, connections_data=None):
    token_data["created_at"] = datetime.now().timestamp()
    if connections_data and len(connections_data) > 0:
        token_data["tenant_id"] = connections_data[0]["tenantId"]
        token_data["tenant_name"] = connections_data[0]["tenantName"]
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)


def load_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    return None

def refresh_access_token(refresh_token):
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    response = requests.post(TOKEN_URL, data=data)
    if response.status_code != 200:
        raise Exception(f"Token refresh failed: {response.text}")

    new_tokens = response.json()
    old_token = load_token()

    if old_token and 'tenant_id' in old_token:
        new_tokens['tenant_id'] = old_token['tenant_id']
        new_tokens['tenant_name'] = old_token['tenant_name']
    save_token(new_tokens)
    session["token"] = new_tokens
    return new_tokens

def get_valid_token():
    token = load_token()
    if not token:
        raise Exception("No Xero token found. Please authenticate first.")

    expires_in = token.get("expires_in", 1800)
    created_at = token.get("created_at", 0)

    if datetime.now().timestamp() - created_at > expires_in - 300:
        return refresh_access_token(token["refresh_token"])
    return token

# Helper: access token string for one-off calls
def get_access_token():
    token = get_valid_token()
    return token["access_token"]

# ============================================
# TENANT MANAGEMENT
# ============================================
def get_tenant_info():
    token = session.get("token") or load_token()
    if not token:
        return None
    if 'tenant_id' in token and 'tenant_name' in token:
        return {'tenantId': token['tenant_id'], 'tenantName': token['tenant_name']}

    access_token = token["access_token"]
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        response = requests.get("https://api.xero.com/connections", headers=headers)
        if response.status_code == 200:
            connections = response.json()
            if connections:
                token['tenant_id'] = connections[0]['tenantId']
                token['tenant_name'] = connections[0]['tenantName']
                save_token(token)
                return connections[0]
    except Exception as e:
        print(f"Error getting tenant info: {e}")
    return None

def get_tenant_id():
    tenant_info = get_tenant_info()
    if not tenant_info:
        raise Exception("No tenant information available. Please re-authenticate.")
    return tenant_info['tenantId']

@app.route("/tenants")
def list_tenants():
    try:
        token = get_valid_token()
        access_token = token["access_token"]
        headers = {"Authorization": f"Bearer {access_token}"}

        response = requests.get("https://api.xero.com/connections", headers=headers)

        if response.status_code == 200:
            tenants = response.json()
            return jsonify({"tenants": tenants, "current_tenant": token.get('tenant_name')})
        else:
            return jsonify({"error": "Failed to fetch tenants"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/switch_tenant", methods=["POST"])
def switch_tenant():
    try:
        tenant_id = request.json.get('tenant_id')
        if not tenant_id:
            return jsonify({"error": "tenant_id is required"}), 400

        token = get_valid_token()
        access_token = token["access_token"]
        headers = {"Authorization": f"Bearer {access_token}"}

        response = requests.get("https://api.xero.com/connections", headers=headers)
        if response.status_code == 200:
            tenants = response.json()
            selected_tenant = next((t for t in tenants if t['tenantId'] == tenant_id), None)
            if selected_tenant:
                token['tenant_id'] = selected_tenant['tenantId']
                token['tenant_name'] = selected_tenant['tenantName']
                save_token(token)
                session["token"] = token
                session["org_name"] = selected_tenant['tenantName']

                return jsonify({"message": f"Switched to {selected_tenant['tenantName']}", "new_tenant": selected_tenant['tenantName']})
            else:
                return jsonify({"error": "Tenant not found"}), 404
        else:
            return jsonify({"error": "Failed to fetch tenants"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============================================
# UTILITY FUNCTIONS
# ============================================
def convert_xero_date(date_string):
    # Accept either /Date(....)/ style or ISO date; return YYYY-MM-DD
    try:
        if not date_string:
            return None
        if isinstance(date_string, str) and date_string.startswith('/Date('):
            ts = int(date_string.split('(')[1].split('+')[0].split('-')[0]) / 1000
            return datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
        # try parse ISO-like
        try:
            return pd.to_datetime(date_string, errors='coerce').strftime('%Y-%m-%d')
        except:
            return date_string
    except:
        return date_string

def format_datetime_for_xero(date_str):
    # input 'YYYY-MM-DD' -> 'YYYY,MM,DD'
    return date_str.replace("-", ",")

# ============================================
# DATA FETCHING FUNCTIONS (CreditNotes & Overpayments)
# ============================================
def fetch_creditnotes_range(start_date, end_date, access_token, tenant_id):
    """Fetch CreditNotes in date range (returns JSON dict)."""
    start_fmt = format_datetime_for_xero(start_date)
    end_fmt = format_datetime_for_xero(end_date)
    url = f"{BASE_URL}/CreditNotes?where=Date>=DateTime({start_fmt}) AND Date<=DateTime({end_fmt})"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "xero-tenant-id": tenant_id,
        "Accept": "application/json"
    }
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        raise Exception(f"CreditNotes fetch failed: {r.status_code} - {r.text}")
    return r.json()

def fetch_overpayments_range(start_date, end_date, access_token, tenant_id):
    """Fetch Overpayments in date range (returns JSON dict). Xero Overpayments supports where filter but to be safe fetch all within small orgs; we'll attempt filtered call first then fallback."""
    start_fmt = format_datetime_for_xero(start_date)
    end_fmt = format_datetime_for_xero(end_date)

    url = f"{BASE_URL}/Overpayments?where=Date>=DateTime({start_fmt}) AND Date<=DateTime({end_fmt})"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "xero-tenant-id": tenant_id,
        "Accept": "application/json"
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()

    # fallback: fetch all and filter
    url_all = f"{BASE_URL}/Overpayments"
    response_all = requests.get(url_all, headers=headers)
    if response_all.status_code != 200:
        raise Exception(f"Overpayments fetch failed: {response_all.status_code} - {response_all.text}")

    # filter by date manually
    all_data = response_all.json()
    filtered = {"Overpayments": []}
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)

    for op in all_data.get("Overpayments", []):
        d = pd.to_datetime(op.get("Date", None), errors='coerce')
        if pd.notna(d) and (d >= start_dt) and (d <= end_dt):
            filtered["Overpayments"].append(op)
    return filtered

# ============================================
# ALLOCATIONS PROCESSING,  Produce DataFrames
# ============================================

def get_credit_note_allocations(start_date, end_date):
    """Get all credit note allocations within date range."""
    token = get_valid_token()
    access_token = token["access_token"]
    tenant_id = get_tenant_id()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Fetch data
    credit_notes_json = fetch_creditnotes_range(start_date, end_date, access_token, tenant_id)
    overpayments_json = fetch_overpayments_range(start_date, end_date, access_token, tenant_id)

    # ---Initialize containers ---
    invoice_alloc = []
    bill_alloc = []
    receive_overpay_alloc = []
    spend_overpay_alloc = []

    credit_notes = credit_notes_json.get("CreditNotes", [])

    for cn in credit_notes:
        cn_number = cn.get("CreditNoteNumber") 
        cn_id = cn.get("CreditNoteID")
        cn_date = convert_xero_date(cn.get("Date"))
        cn_type = cn.get("Type")  # ACCREC / ACCPAY
        
        if not cn_number:
            cn_number = cn.get("Reference") or cn.get("CreditNoteID") 
        
        allocations = cn.get("Allocations") or []   

        for alloc in allocations:
            applied_amount = (
                alloc.get("AppliedAmount") or alloc.get("Amount") or alloc.get("AmountApplied")
            )
            allocation_date = convert_xero_date(alloc.get("Date"))
            invoice_obj = alloc.get("Invoice") or {}
            invoice_number = invoice_obj.get("InvoiceNumber")
            invoice_type = invoice_obj.get("Type")  # ACCREC / ACCPAY / AR/AP OVERPAYMENT

            row = {
                "CreditNoteNumber": cn_number,
                "CreditNoteId" : cn_id,
                "CreditNoteDate": cn_date,
                "AppliedAmount": applied_amount,
                "InvoiceNumber": invoice_number,
                "AllocationType": "Invoice Allocation",
                "Source": "CreditNote",
                
            }

            # --------CATEGORIZATION----------
            if cn_type == "ACCRECCREDIT" or cn_type == "ACCREC":
                row["AllocationType"] = "Invoice Allocation"
                invoice_alloc.append(row)
            elif cn_type == "ACCPAYCREDIT" or cn_type == "ACCPAY":
                row["AllocationType"] = "Bill Allocation"
                bill_alloc.append(row)                
            elif invoice_type == "AROVERPAYMENT":
                row["AllocationType"] = "Receive Overpayment"
                row["OverPaymentType"] = invoice_type
                receive_overpay_alloc.append(row)
            elif invoice_type == "APOVERPAYMENT":
                row["AllocationType"] = "Spend Overpayment"
                row["OverPaymentType"] = invoice_type
                spend_overpay_alloc.append(row)
            else:                    
                # Default to invoice allocation
                row["AllocationType"] = "Invoice Allocation"
                invoice_alloc.append(row)
    # -------------------------
    #   PROCESS OVERPAYMENTS  
    # -------------------------
    overpayments = overpayments_json.get("Overpayments", [])

    for op in overpayments:
        op_number = (
            op.get("Reference")  or op.get("OverpaymentNumber") or op.get("OverpaymentID") or op.get("OverpaymentID")  # UUID wala
        )

        op_id = op.get("OverpaymentID")
        op_date = convert_xero_date(op.get("Date"))
        op_type = op.get("Type")  # RECEIVE-OVERPAYMENT / SPEND-OVERPAYMENT
        allocations = op.get("Allocations") or []

        for alloc in allocations:
            applied_amount = alloc.get("AppliedAmount") or alloc.get("Amount")
            invoice_obj = alloc.get("Invoice") or {}
            invoice_number = invoice_obj.get("InvoiceNumber")

            row = {
                "CreditNoteID": op_id,
                "CreditNoteDate": op_date,
                "AppliedAmount": applied_amount,
                "InvoiceNumber": invoice_number,
                "Source": "Overpayment"
            }

            # Split by RECEIVE vs SPEND
            if op_type == "RECEIVE-OVERPAYMENT":
                receive_overpay_alloc.append(row)
            elif op_type == "SPEND-OVERPAYMENT":
                spend_overpay_alloc.append(row)
            else:
                # Fallback
                receive_overpay_alloc.append(row)

    # Create DataFrames

    df_invoice = pd.DataFrame(invoice_alloc)
    df_bill = pd.DataFrame(bill_alloc)
    df_receive_overpay = pd.DataFrame(receive_overpay_alloc)
    df_spend_overpay = pd.DataFrame(spend_overpay_alloc)

     # Remove unwanted columns if they're empty
    for df in [df_invoice, df_bill, df_receive_overpay, df_spend_overpay]:
        if not df.empty:
            # Drop columns that are all NaN
            df.dropna(axis=1, how='all', inplace=True)

    return {
        "Invoice Allocations": df_invoice,
        "Bill Allocations": df_bill,
        "Receive Overpayment Allocations": df_receive_overpay,
        "Spend Overpayment Allocations": df_spend_overpay
    }

# ============================================
# EXPORT FUNCTIONS
# ============================================
def export_excel_bytes(dfs_dict, start_date, end_date):
    """Create Excel file with separate sheets for each allocation type."""
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in dfs_dict.items():
            sheet_clean = sheet_name[:31]
            if df is None or df.empty:
                pd.DataFrame({"Message": [f"No {sheet_name} found"]}).to_excel(writer, sheet_name=sheet_clean, index=False)
            else:
                df_copy = df.copy()
                # Format date columns
                for col in df_copy.columns:
                    if 'Date' in col:
                        df_copy[col] = pd.to_datetime(df_copy[col], errors='coerce').dt.strftime('%Y-%m-%d')
                df_copy.to_excel(writer, sheet_name=sheet_clean, index=False)
    output.seek(0)
    return output

# ============================================
# ROUTES
# ============================================

@app.route("/")
def home():
    """Home page - check authentication."""
    token = load_token()
    org_name = session.get("org_name")

    if not org_name and token:
        org_name = token.get('tenant_name')
        if org_name:
            session["org_name"] = org_name

    return render_template("index.html", org_name=org_name, is_authenticated=bool(token))

@app.route("/auth")
def auth():
    """Redirect to Xero authentication."""
    auth_url = (
        "https://login.xero.com/identity/connect/authorize"
        "?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        "&scope=offline_access accounting.transactions accounting.contacts"
        "&state=123456"
    )
    return redirect(auth_url)

@app.route("/callback")
def callback():
    """Handle Xero authentication callback."""
    error = request.args.get('error')
    if error:
        error_desc = request.args.get('error_description', 'No description')
        return f"Authentication Failed: {error} - {error_desc}", 400

    code = request.args.get('code')
    if not code:
        return "Missing authorization code", 400
    try:
        # Exchange code for tokens
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
        response = requests.post(TOKEN_URL, data=data)
        if response.status_code != 200:
            return f"Token request failed: {response.status_code} - {response.text}", 400

        token_data = response.json()
        access_token = token_data["access_token"]

        # Get tenant/organization info
        headers = {"Authorization": f"Bearer {access_token}"}
        conn_response = requests.get("https://api.xero.com/connections", headers=headers)

        connections_data = []
        if conn_response.status_code == 200:
            connections_data = conn_response.json()
            if connections_data:
                session["org_name"] = connections_data[0]["tenantName"]

        save_token(token_data, connections_data)
        session["token"] = token_data
        return '''
        <h3> Successfully Connected to Xero!</h3>
        <p><strong>Organization:</strong> {}</p>
        <p>You can now close this window and return to the extraction page.</p>
        <a href="/">Go to Extraction Page</a>
        '''.format(connections_data[0]["tenantName"] if connections_data else "Unknown")

    except Exception as e:
        return f"Authentication failed: {str(e)}", 500


@app.route("/extract/download/<extract_type>")
def extract_and_download(extract_type):
    """Extract and download allocations based on type."""   
    start_date = request.args.get("start")
    end_date = request.args.get("end")

    if not start_date or not end_date:
        return jsonify({"error": "start and end query params are required (YYYY-MM-DD)"}), 400

    valid_types = ["invoice", "bill", "receive_overpayment", "spend_overpayment", "all"]
    if extract_type not in valid_types:
        return jsonify({"error": "Invalid extract type"}), 400

    try:
        # Get all allocation data
        dfs = get_credit_note_allocations(start_date, end_date)

        # Timestamp for filename
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        if extract_type == "all":
            # Download all sheets in one Excel file
            excel_bytes = export_excel_bytes(dfs, start_date, end_date)
            return send_file(excel_bytes, download_name=f"CreditNote_Allocations_{start_date}_to_{end_date}.xlsx", as_attachment=True)
        else:
            # build a single-sheet excel for the requested type
            sheet_map = {
                "invoice": ("Invoice Allocations", f"Invoice_Allocations_{ts}.xlsx"),
                "bill": ("Bill Allocations", f"Bill_Allocations_{ts}.xlsx"),
                "receive_overpayment": ("Receive Overpayment Allocations", f"Receive_Overpayment_Allocations_{ts}.xlsx"),
                "spend_overpayment": ("Spend Overpayment Allocations", f"Spend_Overpayment_Allocations_{ts}.xlsx"),
            }
            sheet_name, filename = sheet_map[extract_type]
            df = dfs.get(sheet_name)
            # if empty -> return a small excel with message
            bytes_io = BytesIO()
            with pd.ExcelWriter(bytes_io, engine="openpyxl") as writer:
                if df is None or df.empty:
                    pd.DataFrame({"Message": [f"No {sheet_name} found for {start_date} to {end_date}"]}).to_excel(writer, sheet_name="Info", index=False)
                else:
                    df_copy = df.copy()
                    for col in df_copy.columns:
                        if "Date" in col:
                            df_copy[col] = pd.to_datetime(df_copy[col], errors="coerce").dt.strftime("%Y-%m-%d")
                    df_copy.to_excel(writer, sheet_name=sheet_name[:31], index=False)

            bytes_io.seek(0)
            return send_file(bytes_io, download_name=filename, as_attachment=True)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/reset")
def reset():
    """Reset authentication - clear tokens and session."""
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
    
    session.clear()
    return """
    <h3>Reset Successful</h3>
    <p>All tokens and session data cleared.</p>
    <a href="/">Go Home</a>
    """

# ============================================
# MAIN
# ============================================
if __name__ == "__main__":
    print("=== XERO CREDIT NOTE EXTRACTOR ===")
    print(f"Client ID: {'✓ Set' if CLIENT_ID else '✗ Missing'}")
    print(f"Client Secret: {'✓ Set' if CLIENT_SECRET else '✗ Missing'}")
    app.run(port=5000, debug=True)
