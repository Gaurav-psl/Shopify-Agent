import React, { useState, useEffect } from "react";
import {
  MessageCircle, Home, Store, Bot, Grid, Palette, BookOpen, HelpCircle,
  Settings, LogOut, Bell, ChevronDown, Info, Search, ShoppingCart, Zap,
  Monitor, Sparkles, RotateCcw, Package, Eye, EyeOff, ArrowRight,
  CheckCircle2, X, Trash2,
} from "lucide-react";

// ---------------------------------------------------------------------
// Point this at your FastAPI server. Same-origin in production — during
// local dev with Vite this can point at http://localhost:8000 while the
// React dev server runs on a different port, as long as CORS +
// credentials are configured on the FastAPI side.
// ---------------------------------------------------------------------
const API_BASE = ""; // e.g. "http://localhost:8000" during separate-port dev

async function apiPost(path, formData, method = "POST") {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    body: formData,
    credentials: "include", // sends/receives the session cookie
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.message || "Request failed");
  return data;
}

async function apiGet(path) {
  const res = await fetch(`${API_BASE}${path}`, { credentials: "include" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function apiDelete(path) {
  const res = await fetch(`${API_BASE}${path}`, { method: "DELETE", credentials: "include" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

// ---------------------------------------------------------------------
// Neutral / grey theme — no accent colors anywhere in the chrome
// ---------------------------------------------------------------------
const BRAND = "#4B5563";       // slate-600, used for primary buttons/icons
const BRAND_SOFT = "#F3F4F6";  // light grey panels
const PAGE_BG = "#F3F4F6";     // light grey page background

const NAV_ITEMS = [
  { id: "overview", label: "Overview", icon: Home },
  { id: "store", label: "Store Information", icon: Store },
  { id: "agent", label: "AI Agent", icon: Bot },
  { id: "features", label: "Features", icon: Grid },
  { id: "appearance", label: "Appearance", icon: Palette },
  { id: "knowledge", label: "Knowledge (FAQs)", icon: BookOpen },
  { id: "feedback", label: "Feedback & Help", icon: HelpCircle },
];

const FEATURE_LIST = [
  { key: "product_search", label: "Product Search", icon: Search },
  { key: "recommendations", label: "Recommendations", icon: Sparkles },
  { key: "product_filtering", label: "Product Filtering", icon: Search },
  { key: "warranty", label: "Warranty", icon: CheckCircle2 },
  { key: "cart_editing", label: "Cart Editing", icon: ShoppingCart },
  { key: "returns", label: "Returns", icon: RotateCcw },
  { key: "track_orders", label: "Track Orders", icon: Package },
];

const STATUS_STYLES = {
  active: { bg: "#E5E7EB", text: "#111827", dot: "#4B5563", label: "Active" },
  inactive: { bg: "#F3F4F6", text: "#9CA3AF", dot: "#D1D5DB", label: "Inactive" },
  maintenance: { bg: "#F3F4F6", text: "#6B7280", dot: "#9CA3AF", label: "Maintenance" },
};

// ---------------------------------------------------------------------
// Small shared UI pieces
// ---------------------------------------------------------------------
function Card({ children, className = "" }) {
  return <div className={`bg-white rounded-2xl border border-gray-100 shadow-sm ${className}`}>{children}</div>;
}

function Toggle({ checked, onChange }) {
  return (
    <button
      onClick={onChange}
      className="relative w-11 h-6 rounded-full transition-colors flex-shrink-0"
      style={{ background: checked ? BRAND : "#E5E7EB" }}
      aria-pressed={checked}
    >
      <span
        className="absolute top-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform"
        style={{ transform: checked ? "translateX(22px)" : "translateX(2px)" }}
      />
    </button>
  );
}

function PageHeader({ title, subtitle }) {
  return (
    <div className="mb-6">
      <h1 className="text-2xl font-bold text-gray-900">{title}</h1>
      {subtitle && <p className="text-gray-500 text-sm mt-1">{subtitle}</p>}
    </div>
  );
}

function TextField({ label, value, onChange, placeholder, type = "text" }) {
  return (
    <label className="block">
      <span className="text-xs font-semibold text-gray-600">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="mt-1.5 w-full px-3 py-2.5 rounded-xl border border-gray-200 text-sm outline-none focus:border-gray-400 focus:ring-2 focus:ring-gray-100 transition"
      />
    </label>
  );
}

// ---------------------------------------------------------------------
// AUTH — Signup / Login
// ---------------------------------------------------------------------
function AuthShell({ children }) {
  return (
    <div className="min-h-screen flex items-center justify-center p-6" style={{ background: PAGE_BG }}>
      <div className="w-full max-w-sm">
        <div className="flex items-center gap-2 justify-center mb-8">
          <div className="w-10 h-10 rounded-xl flex items-center justify-center" style={{ background: BRAND }}>
            <MessageCircle size={20} className="text-white" />
          </div>
          <span className="text-xl font-bold text-gray-900">RenderLink</span>
        </div>
        <Card className="p-7">{children}</Card>
      </div>
    </div>
  );
}

function SignupPage({ defaultShop, onSuccess, goLogin }) {
  const [storeDomain, setStoreDomain] = useState(defaultShop || "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPw, setShowPw] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError("");
    if (!storeDomain.trim() || !email.trim() || password.length < 8) {
      setError("Fill in every field — password needs at least 8 characters.");
      return;
    }
    setLoading(true);
    try {
      const fd = new FormData();
      fd.append("shop", storeDomain.trim());
      fd.append("email", email.trim());
      fd.append("password", password);
      await apiPost("/api/dashboard/signup", fd);
      onSuccess();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthShell>
      <h1 className="text-lg font-bold text-gray-900">Set up your dashboard</h1>
      <p className="text-sm text-gray-500 mt-1 mb-5">Create your login to manage your store's AI assistant.</p>
      <form onSubmit={submit} className="space-y-4">
        <TextField label="Store domain" value={storeDomain} onChange={setStoreDomain} placeholder="myshop.myshopify.com" />
        <TextField label="Email" value={email} onChange={setEmail} placeholder="you@myshop.com" type="email" />
        <label className="block">
          <span className="text-xs font-semibold text-gray-600">Password</span>
          <div className="mt-1.5 relative">
            <input
              type={showPw ? "text" : "password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="At least 8 characters"
              className="w-full px-3 py-2.5 pr-10 rounded-xl border border-gray-200 text-sm outline-none focus:border-gray-400 focus:ring-2 focus:ring-gray-100 transition"
            />
            <button type="button" onClick={() => setShowPw((s) => !s)} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400">
              {showPw ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </label>
        {error && <p className="text-xs text-gray-500">{error}</p>}
        <button disabled={loading} type="submit" className="w-full py-2.5 rounded-xl text-white text-sm font-semibold flex items-center justify-center gap-1.5" style={{ background: BRAND }}>
          {loading ? "Creating…" : <>Create account <ArrowRight size={15} /></>}
        </button>
      </form>
      <p className="text-xs text-gray-500 text-center mt-5">
        Already have an account? <button onClick={goLogin} className="font-semibold text-gray-800">Log in</button>
      </p>
    </AuthShell>
  );
}

function LoginPage({ onSuccess, goSignup }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const fd = new FormData();
      fd.append("email", email);
      fd.append("password", password);
      await apiPost("/api/dashboard/login", fd);
      onSuccess();
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthShell>
      <h1 className="text-lg font-bold text-gray-900">Log in</h1>
      <p className="text-sm text-gray-500 mt-1 mb-5">Welcome back — manage your AI shopping assistant.</p>
      <form onSubmit={submit} className="space-y-4">
        <TextField label="Email" value={email} onChange={setEmail} placeholder="you@myshop.com" type="email" />
        <TextField label="Password" value={password} onChange={setPassword} placeholder="••••••••" type="password" />
        {error && <p className="text-xs text-gray-500">{error}</p>}
        <button disabled={loading} type="submit" className="w-full py-2.5 rounded-xl text-white text-sm font-semibold" style={{ background: BRAND }}>
          {loading ? "Logging in…" : "Log in"}
        </button>
      </form>
      <p className="text-xs text-gray-500 text-center mt-5">
        New here? <button onClick={goSignup} className="font-semibold text-gray-800">Create an account</button>
      </p>
    </AuthShell>
  );
}

// ---------------------------------------------------------------------
// DASHBOARD — sidebar + topbar shell
// ---------------------------------------------------------------------
function Sidebar({ active, setActive, onLogout, store }) {
  return (
    <div className="w-64 bg-white border-r border-gray-100 flex flex-col h-screen sticky top-0">
      <div className="p-5 flex items-center gap-2 border-b border-gray-100">
        <div className="w-9 h-9 rounded-xl flex items-center justify-center" style={{ background: BRAND }}>
          <MessageCircle size={18} className="text-white" />
        </div>
        <div>
          <div className="font-bold text-gray-900 leading-tight">RenderLink</div>
          <div className="text-[11px] text-gray-400 leading-tight">AI Shopping Assistant</div>
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto py-3 px-3 space-y-0.5">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const isActive = active === item.id;
          return (
            <button
              key={item.id}
              onClick={() => setActive(item.id)}
              className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium transition"
              style={{ background: isActive ? BRAND : "transparent", color: isActive ? "#fff" : "#4B5563" }}
            >
              <Icon size={17} />
              {item.label}
            </button>
          );
        })}
        <div className="pt-2 mt-2 border-t border-gray-100">
          <button onClick={() => setActive("settings")} className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm font-medium text-gray-600">
            <Settings size={17} /> Settings
          </button>
        </div>
      </nav>

      <div className="p-3 space-y-3">
        <div className="rounded-2xl p-4" style={{ background: BRAND_SOFT }}>
          <div className="flex items-start gap-2">
            <div className="w-8 h-8 rounded-full bg-white flex items-center justify-center flex-shrink-0">
              <HelpCircle size={15} style={{ color: BRAND }} />
            </div>
            <div>
              <div className="text-xs font-bold text-gray-800">Need help?</div>
              <div className="text-[11px] text-gray-500 mt-0.5">We're here to help you set up and grow.</div>
            </div>
          </div>
          <button onClick={() => setActive("feedback")} className="w-full mt-3 py-2 rounded-lg bg-white text-xs font-semibold" style={{ color: BRAND }}>
            Contact Support
          </button>
        </div>

        <div className="rounded-2xl border border-gray-100 p-3 flex items-center gap-2.5">
          <div className="w-9 h-9 rounded-full text-white text-sm font-bold flex items-center justify-center" style={{ background: BRAND }}>
            {(store.email || "?")[0].toUpperCase()}
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-xs font-bold text-gray-800 truncate">{store.shop_domain}</div>
            <div className="text-[11px] text-gray-500 flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-gray-400" /> Connected
            </div>
          </div>
        </div>

        <button onClick={onLogout} className="w-full flex items-center justify-center gap-2 py-2.5 rounded-xl text-sm font-semibold text-gray-600 border border-gray-200">
          <LogOut size={15} /> Logout
        </button>
      </div>
    </div>
  );
}

function Topbar({ store, agent, onInfoClick, onHelpClick }) {
  const s = STATUS_STYLES[agent.status] || STATUS_STYLES.active;
  return (
    <div className="h-16 bg-white border-b border-gray-100 flex items-center justify-between px-6 sticky top-0 z-10">
      <div className="flex items-center gap-2 text-sm">
        <span className="text-gray-500">Store:</span>
        <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-50 font-medium text-gray-800">
          <Store size={14} /> {store.shop_domain}
        </div>
      </div>
      <div className="flex items-center gap-2.5">
        <span className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-semibold" style={{ background: s.bg, color: s.text }}>
          <span className="w-1.5 h-1.5 rounded-full" style={{ background: s.dot }} /> {s.label}
        </span>
        <button onClick={onInfoClick} className="flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-gray-200 text-xs font-semibold text-gray-600">
          <Info size={13} /> App Info
        </button>
        <button onClick={onHelpClick} className="flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-gray-200 text-xs font-semibold text-gray-600">
          <HelpCircle size={13} /> Help
        </button>
        <button className="relative w-8 h-8 rounded-full border border-gray-200 flex items-center justify-center text-gray-500">
          <Bell size={15} />
        </button>
        <div className="w-8 h-8 rounded-full text-white text-xs font-bold flex items-center justify-center" style={{ background: BRAND }}>
          {(store.email || "?")[0].toUpperCase()}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------
// Enabled Features — dropdown trigger + side panel (replaces the old
// full feature grid on Overview)
// ---------------------------------------------------------------------
function FeaturesDropdown({ features, onToggle }) {
  const [open, setOpen] = useState(false);
  const [panelKey, setPanelKey] = useState(null);
  const panelFeature = FEATURE_LIST.find((f) => f.key === panelKey);

  return (
    <>
      <div className="relative">
        <button
          onClick={() => setOpen((o) => !o)}
          className="w-full flex items-center justify-between px-4 py-3 rounded-xl border border-gray-200 bg-white text-sm font-semibold text-gray-700"
        >
          <span className="flex items-center gap-2"><Grid size={16} /> Enabled Features</span>
          <ChevronDown size={15} className={`text-gray-400 transition-transform ${open ? "rotate-180" : ""}`} />
        </button>

        {open && (
          <div className="absolute left-0 right-0 mt-2 bg-white border border-gray-200 rounded-xl shadow-lg z-20 overflow-hidden">
            {FEATURE_LIST.map((f) => {
              const Icon = f.icon;
              return (
                <button
                  key={f.key}
                  onClick={() => setPanelKey(f.key)}
                  className="w-full flex items-center justify-between px-4 py-2.5 hover:bg-gray-50 text-left border-b border-gray-50 last:border-b-0"
                >
                  <span className="flex items-center gap-2 text-sm text-gray-700"><Icon size={14} /> {f.label}</span>
                  <span className="flex items-center gap-2">
                    <span className="text-[10px] font-semibold text-gray-400">{features[f.key] ? "On" : "Off"}</span>
                    <ArrowRight size={12} className="text-gray-300" />
                  </span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Side panel — appears when a feature row is clicked */}
      {panelFeature && (
        <div className="fixed inset-0 z-40 bg-black/20 flex justify-end" onClick={() => setPanelKey(null)}>
          <div className="w-80 h-full bg-white p-6 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-5">
              <h3 className="font-bold text-gray-900 flex items-center gap-2">
                <panelFeature.icon size={17} /> {panelFeature.label}
              </h3>
              <button onClick={() => setPanelKey(null)} className="text-gray-400"><X size={18} /></button>
            </div>
            <div className="flex items-center justify-between px-4 py-3 rounded-xl bg-gray-50 mb-4">
              <span className="text-sm font-medium text-gray-700">Enabled for this store</span>
              <Toggle checked={features[panelFeature.key]} onChange={() => onToggle(panelFeature.key)} />
            </div>
            <p className="text-xs text-gray-500 leading-relaxed">
              When on, shoppers can ask your AI assistant to {panelFeature.label.toLowerCase()} directly in chat.
            </p>
          </div>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------
// PAGES
// ---------------------------------------------------------------------
function OverviewPage({ agent, features, onToggleFeature, setActive }) {
  return (
    <div className="max-w-3xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Overview</h1>
          <p className="text-gray-500 text-sm mt-1">Here's what's happening with your AI assistant today.</p>
        </div>
      </div>

      <div className="space-y-5">
        {/* AI Agent (name/welcome/status/manage button + embedded Live Preview) */}
        <Card className="p-6">
          <div className="flex items-center gap-2 mb-5">
            <Bot size={18} style={{ color: BRAND }} />
            <h2 className="font-bold text-gray-900">AI Agent</h2>
          </div>
          <div className="flex gap-6">
            <div className="w-20 h-20 rounded-full flex items-center justify-center flex-shrink-0" style={{ background: BRAND_SOFT }}>
              <Bot size={34} style={{ color: BRAND }} />
            </div>
            <div className="flex-1 grid grid-cols-2 gap-6">
              <div>
                <span className="text-xs text-gray-500">Agent Name</span>
                <div className="flex items-center gap-2 mt-0.5">
                  <span className="font-bold text-gray-900">{agent.name}</span>
                  <button onClick={() => setActive("agent")} className="text-[11px] font-semibold px-2 py-0.5 rounded-full" style={{ background: BRAND_SOFT, color: BRAND }}>Edit</button>
                </div>
                <span className="text-xs text-gray-500 block mt-3">Welcome Message</span>
                <div className="text-sm text-gray-700 mt-0.5 bg-gray-50 rounded-lg px-3 py-2">{agent.welcome}</div>
                <span className="text-xs text-gray-500 block mt-3">Status</span>
                <div>
                  <span
                    className="inline-block mt-1 px-2.5 py-1 rounded-full text-[11px] font-semibold"
                    style={{ background: STATUS_STYLES[agent.status].bg, color: STATUS_STYLES[agent.status].text }}
                  >
                    {STATUS_STYLES[agent.status].label}
                  </span>
                </div>
                <button onClick={() => setActive("agent")} className="mt-3 flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold" style={{ background: BRAND_SOFT, color: BRAND }}>
                  <Settings size={13} /> Manage Agent
                </button>
              </div>

              {/* Widget Live Preview */}
              <div>
                <span className="text-xs text-gray-500 flex items-center gap-1.5"><Monitor size={13} /> Widget Live Preview</span>
                <div className="rounded-xl p-3 relative h-32 mt-1.5" style={{ background: BRAND_SOFT }}>
                  <div className="bg-white rounded-xl rounded-tl-none shadow-sm p-2.5 max-w-[85%]">
                    <p className="text-xs text-gray-800">{agent.welcome}</p>
                    <span className="text-[10px] text-gray-400">10:30 AM</span>
                  </div>
                  <div className="absolute bottom-2.5 right-2.5 w-9 h-9 rounded-full flex items-center justify-center shadow-lg" style={{ background: BRAND }}>
                    <MessageCircle size={15} className="text-white" />
                  </div>
                </div>
                <button onClick={() => setActive("appearance")} className="text-xs font-semibold mt-2 flex items-center gap-1 text-gray-700">
                  Open full preview <ArrowRight size={12} />
                </button>
              </div>
            </div>
          </div>
        </Card>

        {/* Enabled Features */}
        <FeaturesDropdown features={features} onToggle={onToggleFeature} />

        {/* Quick Actions */}
        <Card className="p-5">
          <div className="flex items-center gap-2 mb-4">
            <Zap size={17} style={{ color: BRAND }} />
            <h2 className="font-bold text-gray-900 text-sm">Quick Actions</h2>
          </div>
          <div className="space-y-1">
            {[
              { icon: Palette, title: "Customize Appearance", sub: "Change colors, icon & position", page: "appearance" },
              { icon: BookOpen, title: "Manage FAQs", sub: "Add or edit knowledge base", page: "knowledge" },
              { icon: Store, title: "Store Information", sub: "Update your store details", page: "store" },
              { icon: Bot, title: "AI Agent Settings", sub: "Name, instructions & status", page: "agent" },
            ].map((a) => {
              const Icon = a.icon;
              return (
                <button key={a.page} onClick={() => setActive(a.page)} className="w-full flex items-center gap-3 px-3 py-3 rounded-xl hover:bg-gray-50 transition text-left">
                  <div className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0" style={{ background: BRAND_SOFT }}>
                    <Icon size={15} style={{ color: BRAND }} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-bold text-gray-800">{a.title}</div>
                    <div className="text-xs text-gray-500">{a.sub}</div>
                  </div>
                  <ArrowRight size={13} className="text-gray-300" />
                </button>
              );
            })}
          </div>
        </Card>
      </div>

      <p className="text-center text-xs text-gray-400 mt-8">© 2024 RenderLink AI Assistant. All rights reserved.</p>
    </div>
  );
}

function StoreInfoPage({ storeInfo, setStoreInfo }) {
  const [form, setForm] = useState(storeInfo);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { setForm(storeInfo); }, [storeInfo]);

  async function save() {
    setSaving(true);
    setSaved(false);
    setError("");
    try {
      const fd = new FormData();
      fd.append("business_name", form.business_name);
      fd.append("support_email", form.support_email);
      fd.append("timezone", form.timezone);
      const updated = await apiPost("/api/dashboard/store-info", fd);
      setStoreInfo(updated);
      setSaved(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="max-w-xl">
      <PageHeader title="Store Information" subtitle="Basic details about your store, shown when you return to the dashboard." />
      <Card className="p-6 space-y-4">
        <TextField label="Business name" value={form.business_name} onChange={(v) => setForm({ ...form, business_name: v })} placeholder="My Shop Inc." />
        <TextField label="Support email" value={form.support_email} onChange={(v) => setForm({ ...form, support_email: v })} placeholder="support@myshop.com" />
        <TextField label="Timezone" value={form.timezone} onChange={(v) => setForm({ ...form, timezone: v })} placeholder="UTC" />
        {error && <p className="text-xs text-gray-500">{error}</p>}
        <button disabled={saving} onClick={save} className="px-4 py-2.5 rounded-xl text-white text-sm font-semibold" style={{ background: BRAND }}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        {saved && <p className="text-xs text-gray-500 font-medium">Saved ✓</p>}
      </Card>
    </div>
  );
}

function AgentPage({ agent, setAgent }) {
  const [local, setLocal] = useState(agent);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { setLocal(agent); }, [agent]);

  async function save() {
    setSaving(true);
    setSaved(false);
    setError("");
    try {
      const fd = new FormData();
      fd.append("agent_name", local.name);
      fd.append("agent_title", local.welcome);
      fd.append("instructions", local.instructions);
      fd.append("status", local.status);
      const updated = await apiPost("/api/dashboard/agent", fd);
      setAgent(updated);
      setSaved(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="max-w-xl">
      <PageHeader title="AI Agent" subtitle="How your assistant introduces itself, behaves, and whether it's live." />
      <Card className="p-6 space-y-4">
        <TextField label="Agent name" value={local.name} onChange={(v) => setLocal({ ...local, name: v })} />
        <TextField label="Welcome message" value={local.welcome} onChange={(v) => setLocal({ ...local, welcome: v })} />
        <label className="block">
          <span className="text-xs font-semibold text-gray-600">Agent instructions</span>
          <textarea
            value={local.instructions}
            onChange={(e) => setLocal({ ...local, instructions: e.target.value })}
            rows={5}
            className="mt-1.5 w-full px-3 py-2.5 rounded-xl border border-gray-200 text-sm outline-none focus:border-gray-400 focus:ring-2 focus:ring-gray-100 transition"
          />
        </label>
        <div>
          <span className="text-xs font-semibold text-gray-600 block mb-2">Status</span>
          <div className="flex gap-2">
            {["active", "inactive", "maintenance"].map((s) => {
              const style = STATUS_STYLES[s];
              const isSelected = local.status === s;
              return (
                <button
                  key={s}
                  onClick={() => setLocal({ ...local, status: s })}
                  className="px-3 py-2 rounded-lg text-xs font-semibold border flex items-center gap-1.5"
                  style={{ borderColor: isSelected ? style.dot : "#E5E7EB", background: isSelected ? style.bg : "#fff", color: isSelected ? style.text : "#6B7280" }}
                >
                  <span className="w-1.5 h-1.5 rounded-full" style={{ background: style.dot }} /> {style.label}
                </button>
              );
            })}
          </div>
          <p className="text-[11px] text-gray-400 mt-2">"Inactive" or "Maintenance" stops the widget from responding to shoppers until you switch back to Active.</p>
        </div>
        {error && <p className="text-xs text-gray-500">{error}</p>}
        <button disabled={saving} onClick={save} className="px-4 py-2.5 rounded-xl text-white text-sm font-semibold" style={{ background: BRAND }}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        {saved && <span className="ml-3 text-xs text-gray-500 font-medium">Saved ✓</span>}
      </Card>
    </div>
  );
}

function FeaturesPage({ features, onToggle }) {
  return (
    <div className="max-w-2xl">
      <PageHeader title="Features" subtitle="Choose what your AI assistant can do for your customers." />
      <Card className="p-6">
        <div className="grid grid-cols-2 gap-3">
          {FEATURE_LIST.map((f) => {
            const Icon = f.icon;
            return (
              <div key={f.key} className="flex items-center justify-between px-4 py-3 rounded-xl bg-gray-50">
                <span className="flex items-center gap-2 text-sm font-medium text-gray-700"><Icon size={15} /> {f.label}</span>
                <Toggle checked={features[f.key]} onChange={() => onToggle(f.key)} />
              </div>
            );
          })}
        </div>
      </Card>
    </div>
  );
}

function AppearancePage({ agent, setAgent, appearance, setAppearance }) {
  const [color, setColor] = useState(appearance.theme_color);
  const [position, setPosition] = useState(appearance.widget_position);
  const [welcome, setWelcome] = useState(agent.welcome);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    setColor(appearance.theme_color);
    setPosition(appearance.widget_position);
  }, [appearance]);
  useEffect(() => { setWelcome(agent.welcome); }, [agent.welcome]);

  async function save() {
    setSaving(true);
    setSaved(false);
    setError("");
    try {
      const fd = new FormData();
      fd.append("theme_color", color);
      fd.append("widget_position", position);
      const updatedAppearance = await apiPost("/api/dashboard/appearance", fd);
      setAppearance(updatedAppearance);

      if (welcome !== agent.welcome) {
        const fd2 = new FormData();
        fd2.append("agent_name", agent.name);
        fd2.append("agent_title", welcome);
        fd2.append("instructions", agent.instructions);
        fd2.append("status", agent.status);
        const updatedAgent = await apiPost("/api/dashboard/agent", fd2);
        setAgent(updatedAgent);
      }
      setSaved(true);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="max-w-xl">
      <PageHeader title="Appearance" subtitle="Customize how the chat widget looks on your storefront." />
      <Card className="p-6 space-y-5">
        <div>
          <span className="text-xs font-semibold text-gray-600 block mb-2">Theme color</span>
          <div className="flex items-center gap-2">
            {["#4B5563", "#6B7280", "#9CA3AF", "#374151", "#1F2937"].map((c) => (
              <button key={c} onClick={() => setColor(c)} className="w-8 h-8 rounded-full border-2" style={{ background: c, borderColor: color === c ? "#111827" : "transparent" }} />
            ))}
          </div>
        </div>
        <TextField label="Welcome message" value={welcome} onChange={setWelcome} />
        <div>
          <span className="text-xs font-semibold text-gray-600 block mb-2">Widget position</span>
          <div className="flex gap-2">
            {["bottom-right", "bottom-left"].map((p) => (
              <button
                key={p}
                onClick={() => setPosition(p)}
                className="px-3 py-2 rounded-lg text-xs font-semibold border"
                style={{ borderColor: position === p ? BRAND : "#E5E7EB", color: position === p ? BRAND : "#6B7280", background: position === p ? BRAND_SOFT : "#fff" }}
              >
                {p === "bottom-right" ? "Bottom right" : "Bottom left"}
              </button>
            ))}
          </div>
        </div>
        {error && <p className="text-xs text-gray-500">{error}</p>}
        <button disabled={saving} onClick={save} className="px-4 py-2.5 rounded-xl text-white text-sm font-semibold" style={{ background: BRAND }}>
          {saving ? "Saving…" : "Save changes"}
        </button>
        {saved && <span className="ml-3 text-xs text-gray-500 font-medium">Saved ✓</span>}
      </Card>
    </div>
  );
}

function KnowledgePage() {
  const [faqs, setFaqs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [a, setA] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    apiGet("/api/dashboard/faqs")
      .then((d) => setFaqs(d.faqs))
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  async function addFaq() {
    if (!q.trim() || !a.trim()) return;
    setError("");
    try {
      const fd = new FormData();
      fd.append("question", q);
      fd.append("answer", a);
      const created = await apiPost("/api/dashboard/faqs", fd);
      setFaqs([...faqs, created]);
      setQ("");
      setA("");
    } catch (err) {
      setError(err.message);
    }
  }

  async function removeFaq(id) {
    try {
      await apiDelete(`/api/dashboard/faqs/${id}`);
      setFaqs(faqs.filter((f) => f.id !== id));
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <div className="max-w-2xl">
      <PageHeader title="Knowledge (FAQs)" subtitle="Answers your agent can pull from directly." />
      <Card className="p-6 space-y-3 mb-5">
        <TextField label="Question" value={q} onChange={setQ} placeholder="How long does shipping take?" />
        <TextField label="Answer" value={a} onChange={setA} placeholder="3-5 business days." />
        {error && <p className="text-xs text-gray-500">{error}</p>}
        <button onClick={addFaq} className="px-4 py-2.5 rounded-xl text-white text-sm font-semibold" style={{ background: BRAND }}>
          Add FAQ
        </button>
      </Card>
      <div className="space-y-2">
        {loading && <p className="text-xs text-gray-400">Loading…</p>}
        {!loading && faqs.length === 0 && <p className="text-xs text-gray-400">No FAQs yet — add your first one above.</p>}
        {faqs.map((f) => (
          <Card key={f.id} className="p-4 flex items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold text-gray-800">{f.question}</div>
              <div className="text-xs text-gray-500 mt-1">{f.answer}</div>
            </div>
            <button onClick={() => removeFaq(f.id)} className="text-gray-300 hover:text-gray-500 flex-shrink-0">
              <Trash2 size={15} />
            </button>
          </Card>
        ))}
      </div>
    </div>
  );
}

function FeedbackPage() {
  const [msg, setMsg] = useState("");
  const [sent, setSent] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  async function send() {
    if (!msg.trim()) return;
    setSending(true);
    setError("");
    try {
      const fd = new FormData();
      fd.append("message", msg);
      await apiPost("/api/dashboard/feedback", fd);
      setSent(true);
      setMsg("");
    } catch (err) {
      setError(err.message);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="max-w-xl">
      <PageHeader title="Feedback & Help" subtitle="Tell us what's working, what's not, or request a feature." />
      <Card className="p-6 space-y-4">
        <label className="block">
          <span className="text-xs font-semibold text-gray-600">Your message</span>
          <textarea
            value={msg}
            onChange={(e) => setMsg(e.target.value)}
            rows={4}
            placeholder="I'd love to be able to..."
            className="mt-1.5 w-full px-3 py-2.5 rounded-xl border border-gray-200 text-sm outline-none focus:border-gray-400 focus:ring-2 focus:ring-gray-100 transition"
          />
        </label>
        {error && <p className="text-xs text-gray-500">{error}</p>}
        <button disabled={sending} onClick={send} className="px-4 py-2.5 rounded-xl text-white text-sm font-semibold" style={{ background: BRAND }}>
          {sending ? "Sending…" : "Send feedback"}
        </button>
        {sent && <p className="text-xs text-gray-500 font-medium">Thanks — we got it ✓</p>}
      </Card>
    </div>
  );
}

function SettingsPage() {
  return (
    <div className="max-w-xl">
      <PageHeader title="Settings" subtitle="Account-level preferences." />
      <Card className="p-6">
        <p className="text-sm text-gray-500">Nothing here yet — this is a placeholder page.</p>
      </Card>
    </div>
  );
}

// ---------------------------------------------------------------------
// ROOT
// ---------------------------------------------------------------------
export default function App() {
  const [screen, setScreen] = useState("checking"); // checking | signup | login | dashboard
  const [active, setActive] = useState("overview");
  const [infoOpen, setInfoOpen] = useState(false);
  const [loadError, setLoadError] = useState("");

  const [store, setStore] = useState(null);
  const [agent, setAgent] = useState(null);
  const [appearance, setAppearance] = useState(null);
  const [features, setFeatures] = useState(null);
  const [storeInfo, setStoreInfo] = useState(null);

  const shop = new URLSearchParams(window.location.search).get("shop") || "";

  async function loadDashboard() {
    const me = await apiGet("/api/dashboard/me");
    setStore({ shop_domain: me.shop_domain, email: me.email });
    setAgent(me.agent);
    setAppearance(me.appearance);
    setFeatures(me.features);
    setStoreInfo(me.store_info);
    setScreen("dashboard");
  }

  useEffect(() => {
    loadDashboard().catch(() => setScreen(shop ? "signup" : "login"));
  }, []); // eslint-disable-line

  function handleLogout() {
    apiPost("/api/dashboard/logout", new FormData()).finally(() => {
      setStore(null);
      setScreen("login");
      setActive("overview");
    });
  }

  async function toggleFeature(key) {
    const updatedLocal = { ...features, [key]: !features[key] };
    setFeatures(updatedLocal); // optimistic
    try {
      const fd = new FormData();
      Object.entries(updatedLocal).forEach(([k, v]) => fd.append(k, v));
      const saved = await apiPost("/api/dashboard/features", fd);
      setFeatures(saved);
    } catch (err) {
      setFeatures(features); // revert on failure
      setLoadError(err.message);
    }
  }

  if (screen === "checking") {
    return <p className="text-center mt-20 text-sm text-gray-400">Loading…</p>;
  }

  if (screen === "signup") {
    return <SignupPage defaultShop={shop} onSuccess={loadDashboard} goLogin={() => setScreen("login")} />;
  }
  if (screen === "login") {
    return <LoginPage onSuccess={loadDashboard} goSignup={() => setScreen("signup")} />;
  }

  if (!store || !agent || !appearance || !features || !storeInfo) {
    return <p className="text-center mt-20 text-sm text-gray-400">Loading…</p>;
  }

  const pages = {
    overview: <OverviewPage agent={agent} features={features} onToggleFeature={toggleFeature} setActive={setActive} />,
    store: <StoreInfoPage storeInfo={storeInfo} setStoreInfo={setStoreInfo} />,
    agent: <AgentPage agent={agent} setAgent={setAgent} />,
    features: <FeaturesPage features={features} onToggle={toggleFeature} />,
    appearance: <AppearancePage agent={agent} setAgent={setAgent} appearance={appearance} setAppearance={setAppearance} />,
    knowledge: <KnowledgePage />,
    feedback: <FeedbackPage />,
    settings: <SettingsPage />,
  };

  return (
    <div className="flex min-h-screen font-sans" style={{ background: PAGE_BG }}>
      <Sidebar active={active} setActive={setActive} onLogout={handleLogout} store={store} />
      <div className="flex-1 min-w-0">
        <Topbar store={store} agent={agent} onInfoClick={() => setInfoOpen(true)} onHelpClick={() => setActive("feedback")} />
        <div className="p-6">
          {loadError && <p className="text-xs text-gray-500 mb-3">{loadError}</p>}
          {pages[active]}
        </div>
      </div>

      {infoOpen && (
        <div className="fixed inset-0 bg-black/20 flex items-center justify-center z-50" onClick={() => setInfoOpen(false)}>
          <div className="bg-white rounded-2xl p-6 w-full max-w-sm" onClick={(e) => e.stopPropagation()}>
            <h3 className="font-bold text-gray-900 mb-2">App Info</h3>
            <p className="text-sm text-gray-500 mb-1">RenderLink AI Shopping Assistant</p>
            <p className="text-sm text-gray-500 mb-1">Version 1.0.0</p>
            <p className="text-sm text-gray-500">Status: <span className="font-semibold" style={{ color: STATUS_STYLES[agent.status].dot }}>{STATUS_STYLES[agent.status].label}</span></p>
            <button onClick={() => setInfoOpen(false)} className="mt-4 w-full py-2 rounded-lg text-sm font-semibold text-white" style={{ background: BRAND }}>
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
