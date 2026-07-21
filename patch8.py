import re

file_path = '/Volumes/Personal/Learning/rap/backend/phase7.py'
with open(file_path, 'r') as f:
    content = f.read()

target = """        await check_duplicate_application(db, request.requirement_id, consultant.id)

        token = await assert_gmail_connected(db, consultant.id)

        result = await db.execute(select(Requirement).where(Requirement.id == request.requirement_id))"""

replacement = """        await check_duplicate_application(db, request.requirement_id, consultant.id)

        token_res = await db.execute(select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == consultant.id))
        token = token_res.scalars().first()

        result = await db.execute(select(Requirement).where(Requirement.id == request.requirement_id))"""

new_content = content.replace(target, replacement)

target2 = """        access_token = decrypt_token(token.access_token_encrypted)
        send_result = await send_application_email_async(
            access_token=access_token,
            from_email=token.email_address,"""

replacement2 = """        if token and token.access_token_encrypted:
            access_token = decrypt_token(token.access_token_encrypted)
            from_email = token.email_address
        else:
            from gmail_send_service import get_service_account_access_token
            import os
            sa_path = os.path.join(os.path.dirname(__file__), "service-account-key.json")
            from_email = consultant.email
            access_token = get_service_account_access_token(sa_path, from_email)

        send_result = await send_application_email_async(
            access_token=access_token,
            from_email=from_email,"""

new_content = new_content.replace(target2, replacement2)

with open(file_path, 'w') as f:
    f.write(new_content)
print("Patched phase7.py successfully")
