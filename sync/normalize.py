"""Clean one web-form lead before it goes anywhere near the CRM. Returns (lead, problems)."""
import re

import phonenumbers

EMAIL = re.compile(r'^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$')


def _name(s: str) -> str:
    s = ' '.join((s or '').split())
    return s.title() if s.isupper() or s.islower() else s  # fix 'JOHN' / 'john', keep 'McKay' as typed


def normalize(raw: dict) -> tuple[dict, list[str]]:
    problems = []
    email = (raw.get('email') or '').strip().lower()
    if not EMAIL.match(email):
        problems.append(f"invalid email '{raw.get('email')}'")
    first, last = _name(raw.get('first_name', '')), _name(raw.get('last_name', ''))
    if not first and not last:
        problems.append('no name')
    phone, region = (raw.get('phone') or '').strip(), (raw.get('country') or 'US').upper()
    if phone:
        try:
            p = phonenumbers.parse(phone, region)
            phone = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164) if phonenumbers.is_valid_number(p) else ''
        except phonenumbers.NumberParseException:
            phone = ''
        if not phone:
            problems.append(f"invalid phone '{raw.get('phone')}' (dropped, lead kept)")
    lead = {'email': email, 'firstname': first, 'lastname': last, 'phone': phone,
            'company': ' '.join((raw.get('company') or '').split()), 'source': raw.get('source', 'web form')}
    fatal = [p for p in problems if not p.startswith('invalid phone')]
    return lead, fatal if fatal else problems
