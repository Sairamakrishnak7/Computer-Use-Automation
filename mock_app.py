from __future__ import annotations

from flask import Flask, redirect, render_template_string, request, session, url_for

app = Flask(__name__)
app.secret_key = "northstar-local-demo"

MEMBERS = {
    "12345": {
        "name": "Avery Morgan",
        "status": "Active",
        "savings_balance": "$4,820.17",
        "checking_balance": "$1,204.63",
    },
    "67890": {
        "name": "Jordan Lee",
        "status": "Active",
        "savings_balance": "$12,031.44",
        "checking_balance": "$873.22",
    },
}

SHELL = """
<!doctype html>
<html>
<head>
<title>{{ title }}</title>
<style>
body{font-family:Arial,sans-serif;background:#eceff1;margin:0;color:#1d2733}
.shell{width:920px;margin:28px auto;background:#fff;border:1px solid #aab3bd}
.header{background:#17365d;color:#fff;padding:15px 20px;font-size:20px}
.nav{padding:10px 20px;background:#e7ebef;border-bottom:1px solid #b8c0c8}
.nav a{margin-right:20px;color:#17365d}
.content{padding:24px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #bcc4cc;padding:9px;text-align:left}
input,select{padding:7px;min-width:270px}
button,input[type=submit]{padding:8px 14px;min-width:auto}
.notice,.error,.ok{padding:10px;margin-bottom:14px}
.notice{background:#fff7d6;border:1px solid #bc8b00}
.error{background:#fde7e7;border:1px solid #a93d3d}
.ok{background:#e8f6eb;border:1px solid #2f7e40}
.muted{font-size:13px;color:#66717c}
</style>
</head>
<body>
<div class="shell">
<div class="header">Northstar Credit Union — Service Console</div>
<div class="nav"><a href="{{ url_for('home') }}">Home</a><a href="{{ url_for('member_search') }}">Member Search</a></div>
<div class="content">{{ body|safe }}</div>
</div>
</body>
</html>
"""

SEARCH_FORM = """
<h2>Member Search</h2>
<form method="post">
<table>
<tr><td>Member Number</td><td><input id="member-number" name="member_id" aria-label="Member Number" autocomplete="off"></td></tr>
<tr><td></td><td><input type="submit" value="Find Member"></td></tr>
</table>
</form>
"""


def page(title: str, body: str, status: int = 200):
    return render_template_string(SHELL, title=title, body=body), status


@app.get("/")
def home():
    return page("Service Console", "<h2>Operations Home</h2><p>Use Member Search to locate a member and review account information.</p><p class='muted'>Demo application. All data is synthetic.</p>")


@app.route("/members/search", methods=["GET", "POST"])
def member_search():
    if request.method == "GET":
        return page("Member Search", SEARCH_FORM)
    member_id = request.form.get("member_id", "").strip()
    if member_id == "TIMEOUT":
        return page("Session Expired", "<div class='error' data-outcome='session_expired'>Your session has expired. Re-authentication is required.</div><a href='/members/search'>Return to search</a>", 440)
    if member_id == "DENIED":
        return page("Permission Denied", "<div class='error' data-outcome='permission_denied'>You do not have permission to view this member.</div>", 403)
    if member_id not in MEMBERS:
        return page("Member Search", "<div class='notice' data-outcome='member_not_found'>No member was found for that identifier.</div>" + SEARCH_FORM)
    return redirect(url_for("member_detail", member_id=member_id))


@app.get("/members/<member_id>")
def member_detail(member_id: str):
    member = MEMBERS.get(member_id)
    if not member:
        return page("Member", "<div class='notice' data-outcome='member_not_found'>Member not found.</div>", 404)
    body = f"""
<h2>Member Detail</h2>
<table>
<tr><th>Member Number</th><td data-field='member_id'>{member_id}</td></tr>
<tr><th>Name</th><td data-field='member_name'>{member['name']}</td></tr>
<tr><th>Status</th><td>{member['status']}</td></tr>
<tr><th>Savings Balance</th><td data-field='savings_balance'>{member['savings_balance']}</td></tr>
<tr><th>Checking Balance</th><td>{member['checking_balance']}</td></tr>
</table>
<p><a href='/members/{member_id}/subaccounts/new'>Open New Sub-Account</a></p>
"""
    return page("Member Detail", body)


def subaccount_form(member_id: str):
    return f"""
<h2>Open New Sub-Account</h2>
<form method='post'>
<table>
<tr><td>Account Type</td><td><select name='account_type' aria-label='Account Type'><option value=''>-- select --</option><option>Holiday Savings</option><option>Emergency Savings</option></select></td></tr>
<tr><td>Nickname</td><td><input name='nickname' aria-label='Nickname'></td></tr>
<tr><td>Opening Deposit</td><td><input name='opening_deposit' aria-label='Opening Deposit' value='0'></td></tr>
<tr><td></td><td><input type='submit' value='Review Request'></td></tr>
</table>
</form>
<p><a href='/members/{member_id}'>Cancel</a></p>
"""


@app.route("/members/<member_id>/subaccounts/new", methods=["GET", "POST"])
def new_subaccount(member_id: str):
    if member_id not in MEMBERS:
        return page("Member", "<div class='notice' data-outcome='member_not_found'>Member not found.</div>", 404)
    if request.method == "GET":
        return page("New Sub-Account", subaccount_form(member_id))
    account_type = request.form.get("account_type", "")
    nickname = request.form.get("nickname", "").strip()
    opening_deposit = request.form.get("opening_deposit", "").strip()
    if account_type not in {"Holiday Savings", "Emergency Savings"}:
        return page("New Sub-Account", "<div class='error' data-outcome='validation_error'>Select a valid account type.</div>" + subaccount_form(member_id), 400)
    try:
        deposit = float(opening_deposit)
        if deposit < 0:
            raise ValueError
    except ValueError:
        return page("New Sub-Account", "<div class='error' data-outcome='validation_error'>Opening deposit must be zero or greater.</div>" + subaccount_form(member_id), 400)
    session["pending"] = {"member_id": member_id, "account_type": account_type, "nickname": nickname, "opening_deposit": f"{deposit:.2f}"}
    return redirect(url_for("review_subaccount", member_id=member_id))


@app.get("/members/<member_id>/subaccounts/review")
def review_subaccount(member_id: str):
    pending = session.get("pending")
    if not pending or pending.get("member_id") != member_id:
        return redirect(url_for("new_subaccount", member_id=member_id))
    body = f"""
<h2>Review New Sub-Account</h2>
<div class='notice'>Review the request before creating the account.</div>
<table>
<tr><th>Member Number</th><td>{member_id}</td></tr>
<tr><th>Account Type</th><td>{pending['account_type']}</td></tr>
<tr><th>Nickname</th><td>{pending['nickname']}</td></tr>
<tr><th>Opening Deposit</th><td>${pending['opening_deposit']}</td></tr>
</table>
<form method='post' action='/members/{member_id}/subaccounts/create'><button type='submit' data-risk='irreversible'>Create Sub-Account</button></form>
"""
    return page("Review Sub-Account", body)


@app.post("/members/<member_id>/subaccounts/create")
def create_subaccount(member_id: str):
    pending = session.pop("pending", None)
    if not pending:
        return page("Invalid Request", "<div class='error' data-outcome='validation_error'>No pending request exists.</div>", 400)
    return page("Created", "<div class='ok'>Sub-account created.</div><p class='muted'>Synthetic demo action completed.</p>")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
