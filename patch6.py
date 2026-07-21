import re

file_path = '/Volumes/Personal/Learning/rap/backend/main.py'
with open(file_path, 'r') as f:
    content = f.read()

# Add Consultant processing block before `return LoginResponse(...)`
insertion = """
    # Gmail OAuth Token Capture for Consultants
    if user.role == "CONSULTANT":
        from models import Consultant, ConsultantEmailToken
        from gmail_send_service import encrypt_token
        
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3599)
        
        if access_token:
            # Find the consultant record
            cons_result = await db.execute(select(Consultant).where(Consultant.user_id == user.id))
            consultant = cons_result.scalars().first()
            
            if consultant:
                # Find existing token or create new one
                token_result = await db.execute(select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == consultant.id))
                email_token = token_result.scalars().first()
                
                expiry_dt = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                
                if not email_token:
                    email_token = ConsultantEmailToken(
                        consultant_id=consultant.id,
                        access_token_encrypted=encrypt_token(access_token),
                        refresh_token_encrypted=encrypt_token(refresh_token) if refresh_token else None,
                        token_expiry=expiry_dt
                    )
                    db.add(email_token)
                else:
                    email_token.access_token_encrypted = encrypt_token(access_token)
                    if refresh_token:
                        email_token.refresh_token_encrypted = encrypt_token(refresh_token)
                    email_token.token_expiry = expiry_dt
                
                await db.commit()

"""

new_content = content.replace('    return LoginResponse(role=user.role, name=user.full_name, access_token=token)', insertion + '    return LoginResponse(role=user.role, name=user.full_name, access_token=token)')

with open(file_path, 'w') as f:
    f.write(new_content)

print("Patched main.py successfully")
