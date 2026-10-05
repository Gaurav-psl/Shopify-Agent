# """ This file added some code in chatbot_widget file


# Usage:  python patch_chatbot_widget.py path/to/chatbot_widget.py

# Applies the logged-in-name + recommendation-signal changes to your
# chatbot_widget.py. A backup is saved as chatbot_widget.py.bak first.
# Safe to re-run: patches already applied are skipped.
# """
# import re
# import shutil
# import sys

# path = sys.argv[1] if len(sys.argv) > 1 else "chatbot_widget.py"
# src = open(path, encoding="utf-8").read()
# shutil.copy(path, path + ".bak")

# applied, skipped, failed = [], [], []


# def patch(name, old, new, marker):
#     """Replace `old` with `new` exactly once. `marker` is text that only
#     exists after the patch, used to detect an already-applied patch."""
#     global src
#     if marker in src:
#         skipped.append(name)
#         return
#     if src.count(old) != 1:
#         failed.append(f"{name} (found {src.count(old)} matches, expected 1)")
#         return
#     src = src.replace(old, new)
#     applied.append(name)


# # 1. import asyncio
# patch("import asyncio", "import re\nimport time\nimport os\n", "import re\nimport time\nimport os\nimport asyncio\n",
#       "import asyncio")

# # 2. flag key for "name came from the account"
# patch(
#     "ACCOUNT_NAME_KEY",
#     '  var NAME_KEY = "aiChatShopperName_" + SHOP;\n',
#     '  var NAME_KEY = "aiChatShopperName_" + SHOP;\n  var ACCOUNT_NAME_KEY = "aiChatAccountName_" + SHOP;\n',
#     "ACCOUNT_NAME_KEY =",
# )

# # 3. loadCustomerSession: remember account name, forget it on logout
# NEW_LOAD = '''function loadCustomerSession() {
#     var url = CFG.customerSessionPath || "/apps/ai-agent/customer-session";
#     if (url.indexOf("http") !== 0) {
#       if (url.charAt(0) !== "/") url = "/" + url;
#       url += (url.indexOf("?") === -1 ? "?" : "&") + "_ai_session=" + Date.now();
#     }
#     return fetch(url, { credentials: "same-origin", cache: "no-store" })
#       .then(function (res) { return res.json(); })
#       .then(function (data) {
#         if (data && data.authenticated && data.customer_token) {
#           customerToken = data.customer_token; customerAuthenticated = true;
#           try { sessionStorage.setItem(CUSTOMER_TOKEN_KEY, customerToken); } catch (e) {}
#           if (data.first_name) {
#             shopperName = data.first_name;
#             rememberShopperName(data.first_name);
#             try { localStorage.setItem(ACCOUNT_NAME_KEY, "1"); } catch (e) {}
#             refreshGreetingBubble(); showFabGreeting();
#           }
#         } else if (data && !data.error) {
#           // Server answered "not logged in" (not a network/signature error).
#           customerAuthenticated = false; customerToken = null;
#           try { sessionStorage.removeItem(CUSTOMER_TOKEN_KEY); } catch (e) {}
#           var wasAccountName = false;
#           try { wasAccountName = !!localStorage.getItem(ACCOUNT_NAME_KEY); } catch (e) {}
#           if (wasAccountName) {  // they logged out: forget the account's name
#             shopperName = null;
#             try { localStorage.removeItem(NAME_KEY); localStorage.removeItem(ACCOUNT_NAME_KEY); localStorage.removeItem(PROFILE_KEY); } catch (e) {}
#             refreshGreetingBubble(); hideFabGreeting();
#           }
#         }
#         return data;
#       }).catch(function () { return null; });
#   }'''
# if "ACCOUNT_NAME_KEY, \"1\"" in src:
#     skipped.append("loadCustomerSession")
# else:
#     pat = re.compile(r"function loadCustomerSession\(\) \{.*?\.catch\(function \(\) \{ return null; \}\);\s*\n  \}", re.S)
#     if len(pat.findall(src)) == 1:
#         src = pat.sub(lambda m: NEW_LOAD, src)
#         applied.append("loadCustomerSession")
#     else:
#         failed.append("loadCustomerSession (function not found)")

# # 4. logged-in account name wins over a name typed in chat
# patch(
#     "account name wins",
#     "  function tryExtractNameFromMessage(text) {\n    var candidate",
#     "  function tryExtractNameFromMessage(text) {\n    if (customerAuthenticated) return null; // account name wins\n    var candidate",
#     "account name wins",
# )

# # 5. /chat: record search signals for logged-in customers
# patch(
#     "record search in /chat",
#     '    language = classification["language"]\n',
#     '    language = classification["language"]\n'
#     '    if customer_id and intent == "product_search":\n'
#     '        asyncio.create_task(customer_profiles.record_interaction(req.shop, customer_id, dict(entities), customer_profile))\n',
#     "record_interaction(req.shop",
# )

# # 6. follow-up "which item would you like to search for?"
# patch(
#     "record search in follow-up",
#     "        term = extract_search_term(message) or message.strip()\n        result = await _execute_and_reply(store, \"product_search\"",
#     "        term = extract_search_term(message) or message.strip()\n"
#     "        if customer_id:\n"
#     "            asyncio.create_task(customer_profiles.record_interaction(shop, customer_id, {\"query\": term}))\n"
#     "        result = await _execute_and_reply(store, \"product_search\"",
#     "record_interaction(shop, customer_id",
# )

# open(path, "w", encoding="utf-8").write(src)
# print("applied:", applied)
# print("skipped (already applied):", skipped)
# if failed:
#     print("FAILED:", failed)
#     print("Your file differs from what the script expects; the file was still saved with the successful patches. Backup:", path + ".bak")
#     sys.exit(1)
