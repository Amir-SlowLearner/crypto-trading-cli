import ccxt
with open("ccxt-exchanges.txt", "w") as file:
    print("\n".join(ccxt.exchanges), file=file)