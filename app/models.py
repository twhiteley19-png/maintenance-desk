"""Data model for the Maintenance Desk prototype.

Status values on MaintenanceRequest:
  NEW             - triaged, drafts ready, waiting in the approval queue
  NEEDS_HUMAN     - triage was unsure / flagged -> needs manager review first
  NEEDS_HUMAN_NOW - emergency keywords matched -> red banner, call manager now
  APPROVED        - manager approved (mock send, logged)
  REJECTED        - manager rejected
  ESCALATED       - manager escalated for personal handling

Urgency values: EMERGENCY | URGENT | ROUTINE
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from .db import Base

# --- status / urgency vocab -------------------------------------------------
STATUS_NEW = "NEW"
STATUS_NEEDS_HUMAN = "NEEDS_HUMAN"
STATUS_NEEDS_HUMAN_NOW = "NEEDS_HUMAN_NOW"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_ESCALATED = "ESCALATED"

URGENCY_EMERGENCY = "EMERGENCY"
URGENCY_URGENT = "URGENT"
URGENCY_ROUTINE = "ROUTINE"


class Property(Base):
    __tablename__ = "properties"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    address = Column(String(255), nullable=False)
    owner_spend_limit = Column(Float, default=500.0)

    units = relationship("Unit", back_populates="property", cascade="all, delete-orphan")


class Unit(Base):
    __tablename__ = "units"

    id = Column(Integer, primary_key=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False)
    label = Column(String(40), nullable=False)  # e.g. "Apt 3B"

    property = relationship("Property", back_populates="units")
    tenants = relationship("Tenant", back_populates="unit", cascade="all, delete-orphan")


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True)
    unit_id = Column(Integer, ForeignKey("units.id"), nullable=False)
    name = Column(String(120), nullable=False)
    phone = Column(String(40), nullable=False, unique=True)

    unit = relationship("Unit", back_populates="tenants")
    requests = relationship("MaintenanceRequest", back_populates="tenant")


class Vendor(Base):
    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True)
    trade = Column(String(40), nullable=False)  # plumbing, electrical, hvac, ...
    name = Column(String(120), nullable=False)
    phone = Column(String(40), nullable=False)
    priority = Column(Integer, default=1)  # lower number = preferred first
    after_hours_ok = Column(Integer, default=0)  # 0/1


class MaintenanceRequest(Base):
    __tablename__ = "requests"

    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    raw_message = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    category = Column(String(40), default="unknown")
    urgency = Column(String(20), default=URGENCY_ROUTINE)
    status = Column(String(20), default=STATUS_NEW)

    ai_summary = Column(Text, default="")
    follow_up_questions = Column(Text, default="")  # newline-separated
    draft_tenant_reply = Column(Text, default="")
    draft_vendor_message = Column(Text, default="")
    chosen_vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)
    needs_human_reason = Column(Text, default="")

    # --- Phase 1 fields -------------------------------------------------
    # 1 while a routine/urgent request waits for morning (after-hours hold).
    after_hours_hold = Column(Integer, default=0)
    # Emergency alert loop: 1 once the manager replies ACK.
    emergency_acknowledged = Column(Integer, default=0)
    last_alert_at = Column(DateTime, nullable=True)
    # Vendor ack/fallback loop.
    vendor_dispatch_sent_at = Column(DateTime, nullable=True)
    vendor_confirmed = Column(Integer, default=0)
    tried_vendor_ids = Column(Text, default="")  # comma-separated vendor ids
    fallback_count = Column(Integer, default=0)
    # Next-day fix check.
    fix_check_due_at = Column(DateTime, nullable=True)
    fix_check_sent_at = Column(DateTime, nullable=True)
    fix_check_reply = Column(String(10), nullable=True)  # YES / NO / None

    tenant = relationship("Tenant", back_populates="requests")
    chosen_vendor = relationship("Vendor")
    log = relationship("MessageLog", back_populates="request", cascade="all, delete-orphan")


class MessageLog(Base):
    """Log EVERYTHING: inbound tenant text, triage notes, drafts, approvals."""

    __tablename__ = "message_log"

    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    direction = Column(String(20), nullable=False)  # in | out | system
    body = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

    request = relationship("MaintenanceRequest", back_populates="log")
