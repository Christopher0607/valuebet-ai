"""
Database models — SQLite via SQLAlchemy.
Mirrors the structure we designed for Supabase, but self-contained
in a single file on disk (valuebet.db), no external service needed.
"""
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, Date, DateTime, ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime

DATABASE_URL = "sqlite:///./valuebet.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Competition(Base):
    __tablename__ = "competitions"
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)          # 'wc2026' | 'ucl2627'
    name = Column(String, nullable=False)
    name_zh = Column(String)
    data_source = Column(String)                                  # results feed URL
    odds_sport_key = Column(String)                                # unused locally, kept for parity
    is_active = Column(Boolean, default=True)


class Match(Base):
    __tablename__ = "matches"
    id = Column(Integer, primary_key=True)
    competition_id = Column(Integer, ForeignKey("competitions.id"))
    date = Column(Date, nullable=False)
    time_utc = Column(String)
    team1 = Column(String, nullable=False)
    team2 = Column(String, nullable=False)
    score1 = Column(Integer, nullable=True)
    score2 = Column(Integer, nullable=True)
    round = Column(String)
    grp = Column(String)
    ground = Column(String)
    status = Column(String, default="upcoming")                    # 'upcoming' | 'played'
    bayesian_folded_in = Column(Boolean, default=False)
    # Tracks whether this match's result has already been folded into its
    # two teams' Bayesian posteriors. Without this, update_bayesian_states_
    # for_newly_played_matches() re-applies every played match's score on
    # every run -- measured directly: three consecutive manual "update now"
    # calls with zero new matches still moved Mexico's attack estimate from
    # 1.1606 to 1.2124 to 1.2522, purely from re-processing the same results.
    # This column is the actual fix, not just a documented caveat.
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    competition = relationship("Competition")


class Prediction(Base):
    __tablename__ = "predictions"
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"), unique=True)
    model = Column(String, default="dixon-coles")
    prob_home = Column(Float)
    prob_draw = Column(Float)
    prob_away = Column(Float)
    xg_home = Column(Float)
    xg_away = Column(Float)
    attack_home = Column(Float)
    defense_home = Column(Float)
    attack_away = Column(Float)
    defense_away = Column(Float)
    predicted = Column(String)                                      # 'win1' | 'draw' | 'win2'
    is_correct = Column(Boolean, nullable=True)
    rps = Column(Float, nullable=True)

    match = relationship("Match")


class Odds(Base):
    __tablename__ = "odds"
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"))
    source = Column(String, default="manual")
    odds_home = Column(Float)
    odds_draw = Column(Float, nullable=True)
    odds_away = Column(Float)
    recorded_at = Column(DateTime, default=datetime.utcnow)


class Bet(Base):
    """Virtual bets — for mathematically testing the model, not real money."""
    __tablename__ = "bets"
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"))
    outcome = Column(String, nullable=False)                        # 'home' | 'draw' | 'away'
    stake = Column(Float, default=100)
    odds_used = Column(Float)
    ev_at_bet = Column(Float)
    kelly_pct = Column(Float)
    model_prob = Column(Float)
    result = Column(String, default="pending")                      # 'win' | 'loss' | 'pending'
    pnl = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    match = relationship("Match")


class RealBet(Base):
    """Real-money bets, entered manually after you place them on BK8/etc.
    This app never places bets automatically — see the top-level README
    for why that's intentionally not something this system does."""
    __tablename__ = "real_bets"
    id = Column(Integer, primary_key=True)
    match_id = Column(Integer, ForeignKey("matches.id"))
    competition_id = Column(Integer, ForeignKey("competitions.id"))
    platform = Column(String, default="bk8")
    outcome = Column(String, nullable=False)
    stake_real = Column(Float, nullable=False)
    currency = Column(String, default="HKD")
    odds_used = Column(Float, nullable=False)
    model_prob_at_bet = Column(Float)
    ev_at_bet = Column(Float)
    kelly_suggested_pct = Column(Float)
    kelly_suggested_amount = Column(Float)
    actually_followed_kelly = Column(Boolean, nullable=True)
    result = Column(String, default="pending")
    payout_real = Column(Float, nullable=True)
    pnl_real = Column(Float, nullable=True)
    placed_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)

    match = relationship("Match")


class ParlayBet(Base):
    """
    串关注单（虚拟盘和实盘共用一张表，用 kind 区分）。

    为什么不像 Bet/RealBet 那样拆成两张表：串关的结算逻辑（所有腿全中才算赢、
    任何一腿输了立刻判负）比单场复杂得多，拆两张表意味着这套逻辑要写两遍、
    以后改也要改两处。用一个 kind 字段区分，结算逻辑只写一次。

    为什么单场注单不能装串关：Bet.match_id 是单场比赛的外键，一行对应一场。
    串关是 3-8 场，现有表结构表达不了——这才是之前串关页没有「记录注单」
    按钮的真实原因，不是漏做了 UI，是数据模型根本存不下。
    """
    __tablename__ = "parlay_bets"
    id = Column(Integer, primary_key=True)
    kind = Column(String, nullable=False, default="virtual")        # 'virtual' | 'real'
    stake = Column(Float, nullable=False)
    odds_used = Column(Float, nullable=False)
    # 实际串关总赔率。通常约等于各腿赔率相乘，但博彩公司可能有自己的串关定价，
    # 所以允许用户在记录实盘时填入真实拿到的总赔率，而不是强制用乘积。
    joint_probability = Column(Float)
    ev_at_bet = Column(Float)
    kelly_pct = Column(Float)
    platform = Column(String, default="bk8")                         # 仅 kind='real' 时有意义
    currency = Column(String, default="HKD")
    result = Column(String, default="pending")                       # 'pending' | 'win' | 'loss'
    pnl = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)

    legs = relationship("ParlayLeg", back_populates="parlay", cascade="all, delete-orphan")


class ParlayLeg(Base):
    """串关的单条腿。一注串关有 2-8 条。"""
    __tablename__ = "parlay_legs"
    id = Column(Integer, primary_key=True)
    parlay_bet_id = Column(Integer, ForeignKey("parlay_bets.id", ondelete="CASCADE"), nullable=False)
    match_id = Column(Integer, ForeignKey("matches.id"), nullable=False)
    outcome = Column(String, nullable=False)                         # 'home' | 'draw' | 'away'
    leg_odds = Column(Float)
    leg_prob = Column(Float)

    parlay = relationship("ParlayBet", back_populates="legs")
    match = relationship("Match")


class UserSettings(Base):
    __tablename__ = "user_settings"
    id = Column(Integer, primary_key=True)
    setting_key = Column(String, unique=True, default="default")
    bankroll_total = Column(Float, default=10000)
    kelly_fraction = Column(Float, default=0.5)
    max_bet_pct = Column(Float, default=0.15)
    min_ev_threshold = Column(Float, default=0.03)


class BayesianTeamStateRow(Base):
    """
    Persists BayesianTeamState (see model.py) across backend restarts.
    Without this table, every posterior update would live only in memory
    and vanish the moment uvicorn restarts -- which would make "real-time
    Bayesian updating" a lie in practice, since the whole point is that a
    team's parameters keep drifting with recent form across many matches,
    not just within a single process lifetime.

    One row per (team_name, competition_id) -- a team's Bayesian state is
    scoped to a competition, since e.g. Spain's national-team form and
    a Spanish club's league form are unrelated quantities.
    """
    __tablename__ = "bayesian_team_states"
    id = Column(Integer, primary_key=True)
    team_name = Column(String, nullable=False)
    competition_id = Column(Integer, ForeignKey("competitions.id"), nullable=False)
    attack_shape = Column(Float, nullable=False)
    attack_rate = Column(Float, nullable=False)
    defense_theta_shape = Column(Float, nullable=False)
    defense_theta_rate = Column(Float, nullable=False)
    decay = Column(Float, default=0.98)
    n_updates = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("team_name", "competition_id", name="uq_team_competition"),
    )


class UpdateLog(Base):
    """Every scheduler run writes a row here, so the frontend can show
    'last updated' / 'next update' honestly instead of just claiming it."""
    __tablename__ = "update_log"
    id = Column(Integer, primary_key=True)
    ran_at = Column(DateTime, default=datetime.utcnow)
    matches_updated = Column(Integer, default=0)
    predictions_updated = Column(Integer, default=0)
    bets_resolved = Column(Integer, default=0)
    status = Column(String, default="ok")                           # 'ok' | 'error'
    detail = Column(String, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
