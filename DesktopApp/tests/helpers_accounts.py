"""Select a real vault record in connection-dialog regression tests."""
def select_account(dialog, username='alice', password='fixture-private-password', *, remember=False):
    store = dialog.store
    account = next((a for a in store.accounts() if a['name'] == username), None)
    account = store.put_account(username, username, password,
        account_id=(account or {}).get('id'), remember_password=remember)
    dialog.reload_accounts(account['id'])
    return account
