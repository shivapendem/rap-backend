import re

file_path = '/Volumes/Personal/Learning/rap/backend/main.py'
with open(file_path, 'r') as f:
    content = f.read()

target = """                        from gmail_send_service import get_service_account_access_token
                        import os
                        
                        sa_path = os.path.join(os.path.dirname(__file__), "service-account-key.json")
                        access_token = get_service_account_access_token(sa_path, item.from_email)"""

replacement = """                        from gmail_send_service import get_service_account_access_token, decrypt_token
                        from models import User, Consultant, ConsultantEmailToken
                        import os
                        
                        access_token = None
                        
                        # 1. Try Consultant OAuth Token First
                        user_res = await session.execute(select(User).where(User.email == item.from_email))
                        from_user = user_res.scalars().first()
                        if from_user and from_user.role == "CONSULTANT":
                            cons_res = await session.execute(select(Consultant).where(Consultant.user_id == from_user.id))
                            cons = cons_res.scalars().first()
                            if cons:
                                tok_res = await session.execute(select(ConsultantEmailToken).where(ConsultantEmailToken.consultant_id == cons.id))
                                email_tok = tok_res.scalars().first()
                                if email_tok and email_tok.access_token_encrypted:
                                    access_token = decrypt_token(email_tok.access_token_encrypted)
                        
                        # 2. Fallback to Domain Delegation
                        if not access_token:
                            sa_path = os.path.join(os.path.dirname(__file__), "service-account-key.json")
                            access_token = get_service_account_access_token(sa_path, item.from_email)"""

new_content = content.replace(target, replacement)
with open(file_path, 'w') as f:
    f.write(new_content)
print("Patched main.py for email send fallback")
