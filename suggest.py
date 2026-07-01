import difflib
from config import COIN_IDS

def suggest_symbol(user_input):
    """输错币名时，返回最接近的建议"""
    user_input = user_input.upper()
    # 在所有币种符号里找最接近的
    matches = difflib.get_close_matches(user_input, COIN_IDS.keys(), n=3, cutoff=0.5)
    return matches
