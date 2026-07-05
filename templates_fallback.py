"""
Fallback templates when ALL AI providers are down.
These are last-resort, not the primary path.
Humanized version: lowercase, slang, typos, "sent from iphone"
"""

import random


def generate_fallback_opener(creator_info: dict) -> dict:
    name = creator_info.get("name") or "there"
    if name and " " in name:
        name = name.split()[0]  # First name only
    
    name = name.lower()

    openers = [
        f"yo {name}, been seeing ur stuff and love what u r doing.",
        f"hey {name}, came across ur profile and ur content is crazy good.",
        f"yo {name}, ur content caught my eye wanted to reach out quick.",
    ]
    
    sign_offs = [
        "\n\nsent from my iphone",
        "\n\nsent from iphone",
        "",
        ""
    ]

    body = (
        f"{random.choice(openers)}\n\n"
        f"i work w magicfit ai, we turn product links into ugc style ads automatically. "
        f"are u open to collabs rn?\n\n"
        f"we do upfront flat fee + 50% commision on referrals for 12 months. "
        f"happy to share more if ur interested.\n\n"
        f"lmk either way!"
        f"{random.choice(sign_offs)}"
    )

    subjects = [
        "collab opportunity",
        "quick collab idea",
        "partnership",
    ]

    return {"subject": random.choice(subjects), "body": body}


def generate_fallback_followup(creator_info: dict, followup_number: int) -> dict:
    name = creator_info.get("name") or "there"
    if name and " " in name:
        name = name.split()[0]
    
    name = name.lower()
    
    sign_offs = [
        "\n\nsent from my iphone",
        "\n\nsent from iphone",
        "",
        ""
    ]

    if followup_number == 1:
        body = (
            f"hey {name}, just bumping this up in case u missed it.\n\n"
            f"would love to hear if ur open to the collab. totally no pressure tho.\n\n"
            f"best,\nrajdeep"
            f"{random.choice(sign_offs)}"
        )
    elif followup_number == 2:
        body = (
            f"yo {name}, circling back one last time.\n\n"
            f"if timing is off no worries at all, but if ur curious id love to chat.\n\n"
            f"best,\nrajdeep"
            f"{random.choice(sign_offs)}"
        )
    else:
        body = (
            f"hey {name}, final follow up from me promise!\n\n"
            f"if its not a fit rn totally get it. hmu anytime if things change.\n\n"
            f"best,\nrajdeep"
            f"{random.choice(sign_offs)}"
        )

    return {"subject": "re: collab opportunity", "body": body}


def generate_fallback_reply(creator_info: dict, instruction: str = "") -> dict:
    import json
    import database as db

    name = creator_info.get("name") or "there"
    if name and " " in name:
        name = name.split()[0]
        
    name = name.lower()

    tier = creator_info.get("tier") or "unknown"
    deal_json = db.get_setting("deal_structure", "{}")
    try:
        deal = json.loads(deal_json)
    except Exception:
        deal = {}
    tier_deal = deal.get(tier) or deal.get("under_50k") or {}

    flat_fee = tier_deal.get("flat_fee", 100)
    commission = tier_deal.get("commission_pct", 50)
    months = tier_deal.get("commission_months", 12)
    
    sign_offs = [
        "\n\nsent from my iphone",
        "\n\nsent from iphone",
        "",
        ""
    ]

    body = (
        f"hey {name}, thanks for getting back!\n\n"
        f"heres the structure: ${flat_fee} flat fee upfront, {commission}% commission on paying referrals for {months} months, "
        f"so u keep earning way after the post goes live.\n\n"
        f"if that sounds fair, i can send over the link to get u setup. how does that sound?"
        f"{random.choice(sign_offs)}"
    )

    return {"subject": "re: collab opportunity", "body": body}
