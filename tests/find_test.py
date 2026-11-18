with open("c:\\Projects\\SiloVault\\tests\\test_recovery.py", "r") as f:
    c = f.read()

idx = c.find("def test_two_sessions_compete_exactly_one_claim")
print("Found at position:", idx)
print("Context:")
print(c[idx:idx+2000])