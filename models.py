"""
models.py
---------
Three tables:

Store
    One row per Shopify store that has installed the app. Holds the
    Admin API access token issued during OAuth — this is what lets your
    server call Shopify's API on that store's behalf (orders, products,
    carts, etc).

DashboardUser
    Login credentials for the store owner to access their settings
    dashboard (separate from their Shopify login — this is your app's
    own login).

AgentCustomization
    Per-store settings the owner controls from the dashboard: the
    chatbot's display name/title/description, and its button icon
    (either a custom uploaded image, or a preset icon recolored via
    theme_color).
"""

import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship

from database import Base


class Store(Base):
    __tablename__ = "stores"

    id = Column(Integer, primary_key=True)
    shop_domain = Column(String(255), unique=True, nullable=False, index=True)  # e.g. "example.myshopify.com"
    access_token = Column(String(255), nullable=False)
    scopes = Column(String(500), default="")
    installed_at = Column(DateTime, default=datetime.datetime.utcnow)
    uninstalled = Column(Boolean, default=False)

    dashboard_user = relationship("DashboardUser", back_populates="store", uselist=False)
    customization = relationship("AgentCustomization", back_populates="store", uselist=False)


class DashboardUser(Base):
    __tablename__ = "dashboard_users"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    store = relationship("Store", back_populates="dashboard_user")


class AgentCustomization(Base):
    __tablename__ = "agent_customizations"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"), unique=True, nullable=False)

    agent_name = Column(String(100), default="AI Assistant")
    agent_title = Column(String(150), default="How can I help you today?")
    agent_description = Column(Text, default="")

    # Button icon: either "preset" (a built-in icon recolored via theme_color)
    # or "custom" (a store-uploaded image at custom_icon_url).
    icon_type = Column(String(20), default="preset")  # "preset" | "custom"
    theme_color = Column(String(20), default="#2b2b2b")
    custom_icon_url = Column(String(500), default="")

    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    store = relationship("Store", back_populates="customization")
