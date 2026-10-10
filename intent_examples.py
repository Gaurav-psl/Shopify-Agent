"""
intent_examples.py
------------------
Example phrases per (intent, action), used by semantic.route_intent() to
find the closest meaning when the LLM classifier gives up.

More and more varied examples = better matching. Add real messages from
your logs here (especially ones that ended up as "fallback"). Keep a mix of
English, Hinglish (Hindi in English letters), Hindi and Punjabi, matching
what your shoppers actually write.

Only some actions are ever *acted on* by the router (see _RESCUABLE in
chatbot_widget.py). The others (cart changes, claims, login...) are listed
anyway so that a message like "remove this from my cart" is recognised as
that, instead of being mistaken for something harmless.
"""

EXAMPLES: dict[tuple[str, str], list[str]] = {
    ("customer_account", "get_my_orders"): [
        "show all my orders", "what have I ordered so far", "show my order history",
        "list my past and current orders", "which orders have I placed", "do I have any pending orders",
        "what did I buy from you before", "show my previous purchases",
        "mere saare orders dikhao", "meri purani orders ki list dikhao", "maine ab tak kya kya order kiya hai",
        "मेरे सारे ऑर्डर दिखाओ", "मेरे पिछले ऑर्डर की जानकारी दो",
        "ਮੇਰੇ ਸਾਰੇ ਆਰਡਰ ਦਿਖਾਓ", "ਮੈਂ ਹੁਣ ਤੱਕ ਕੀ ਆਰਡਰ ਕੀਤਾ ਹੈ", "ਮੇਰੇ ਪੁਰਾਣੇ ਆਰਡਰ ਦੀ ਲਿਸਟ ਦਿਖਾਓ",
    ],
    ("customer_account", "get_recommendations"): [
        "recommend something for me", "what would you suggest for me", "suggest products I might like",
        "what should I buy next", "any picks based on my past orders", "show me things similar to what I bought",
        "I don't know what to get, help me choose", "surprise me with something I'd like",
        "mere liye kuch recommend karo", "mujhe kya lena chahiye", "mere pasand ka kuch dikhao",
        "मेरे लिए कुछ सुझाइए", "मुझे क्या खरीदना चाहिए",
        "ਮੇਰੇ ਲਈ ਕੁਝ ਸੁਝਾਓ", "ਮੈਨੂੰ ਕੀ ਖਰੀਦਣਾ ਚਾਹੀਦਾ ਹੈ",
    ],
    ("product_search", "search_products"): [
        "show me running shoes", "do you have anything in blue", "I'm looking for something warm for winter",
        "what's your cheapest t-shirt", "find me a gift under 1000 rupees", "browse your collection",
        "anything good for summer", "I need something to wear to a wedding",
        "kya aapke paas hoodie hai", "mujhe ek achi t-shirt dikhao", "sasta jacket dikhao",
        "मुझे जूते दिखाओ", "कोई अच्छी टी-शर्ट है क्या",
        "ਮੈਨੂੰ ਟੀ-ਸ਼ਰਟ ਦਿਖਾਓ", "ਕੀ ਤੁਹਾਡੇ ਕੋਲ ਹੂਡੀ ਹੈ",
    ],
    ("order_tracking", "track_order"): [
        "where is my order", "track order 1042", "has my package shipped yet", "when will my order arrive",
        "delivery status of my parcel", "my order hasn't arrived",
        "mera order kahan hai", "mere order ka status batao", "parcel kab tak aayega",
        "मेरा ऑर्डर कहाँ है", "ਮੇਰਾ ਆਰਡਰ ਕਿੱਥੇ ਹੈ", "ਮੇਰਾ ਪਾਰਸਲ ਕਦੋਂ ਆਵੇਗਾ",
    ],
    ("cart_management", "view_cart"): [
        "what's in my cart", "show my cart", "what have I added to my basket", "open my bag",
        "mera cart dikhao", "cart mein kya hai", "मेरा कार्ट दिखाओ", "ਮੇਰਾ ਕਾਰਟ ਦਿਖਾਓ",
    ],
    ("cart_management", "add_item"): [
        "add this to my cart", "put the blue hoodie in my bag", "I want to buy this one",
        "isse cart mein daalo", "yeh wali shirt cart mein add karo", "इसे कार्ट में डालो", "ਇਹ ਕਾਰਟ ਵਿੱਚ ਪਾਓ",
    ],
    ("cart_management", "remove_item"): [
        "remove the shirt from my cart", "take that out of my basket", "I don't want the hoodie anymore",
        "isse cart se hatao", "इसे कार्ट से हटाओ",
    ],
    ("cart_management", "edit_quantity"): [
        "change the quantity to 2", "make it three of those", "I need two instead of one",
        "quantity badal ke 2 kar do",
    ],
    ("cart_management", "clear_cart"): [
        "empty my cart", "remove everything from my cart", "clear my basket", "cart khali karo", "कार्ट खाली करो",
    ],
    ("warranty_claim", "submit_claim"): [
        "this arrived broken and I want a replacement", "I want to return my order", "the item is damaged, how do I claim warranty",
        "I received the wrong size, I want my money back", "mujhe return karna hai", "product kharab aaya hai",
        "प्रोडक्ट खराब आया है", "ਸਮਾਨ ਖਰਾਬ ਆਇਆ ਹੈ",
    ],
    ("warranty_claim", "check_claim_status"): [
        "what's the status of my warranty claim", "has my return been approved", "any update on my replacement request",
    ],
    ("policy_query", "answer_policy_question"): [
        "what's your refund policy", "how long does shipping take", "do you ship internationally",
        "can I return an item after 30 days", "do you share my data with anyone", "what are the delivery charges",
        "return policy kya hai", "shipping mein kitna time lagta hai", "delivery charges kitne hain",
        "आपकी रिफंड पॉलिसी क्या है", "ਤੁਹਾਡੀ ਰਿਫੰਡ ਪਾਲਿਸੀ ਕੀ ਹੈ",
    ],
    ("registration_login", "register"): [
        "I want to create an account", "sign me up", "how do I register", "account banana hai",
    ],
    ("registration_login", "login"): [
        "log me in", "I want to sign in", "how do I log into my account", "login kaise karu",
    ],
    ("registration_login", "logout"): [
        "sign me out", "log out of my account", "logout karna hai",
    ],
    ("registration_login", "forgot_password"): [
        "I forgot my password", "reset my password", "can't log in, need a new password", "password bhool gaya",
    ],
    ("fallback", "clarify"): [
        "hmm", "can you help me with something", "asdkfj", "hello", "hi there", "ok", "thanks", "what can you do",
    ],
}
