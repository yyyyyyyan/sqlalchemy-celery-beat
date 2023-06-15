# coding=utf-8

import datetime as dt
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from celery import schedules
from celery.utils.log import get_logger
from sqlalchemy import func
from sqlalchemy.event import listen, listens_for
from sqlalchemy.orm import foreign, relationship, remote, validates
from sqlalchemy.sql import insert, select, update

from .clockedschedule import clocked
from .session import ModelBase
from .tzcrontab import TzAwareCrontab

logger = get_logger('sqlalchemy_celery_beat.models')


def cronexp(field):
    """Representation of cron expression."""
    return field and str(field).replace(' ', '') or '*'


class ModelMixin(object):

    @classmethod
    def create(cls, **kw):
        return cls(**kw)

    def update(self, **kw):
        for attr, value in kw.items():
            setattr(self, attr, value)
        return self


class IntervalSchedule(ModelBase, ModelMixin):
    __tablename__ = 'celery_interval_schedule'
    __table_args__ = {
        'sqlite_autoincrement': True,
        'schema': 'celery_schema'
    }

    DAYS = 'days'
    HOURS = 'hours'
    MINUTES = 'minutes'
    SECONDS = 'seconds'
    MICROSECONDS = 'microseconds'

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)

    every = sa.Column(sa.Integer, nullable=False)
    period = sa.Column(sa.String(24))

    def __repr__(self):
        if self.every == 1:
            return 'every {0}'.format(self.period_singular)
        return 'every {0} {1}'.format(self.every, self.period)

    @property
    def schedule(self):
        return schedules.schedule(
            dt.timedelta(**{self.period: self.every}),
            # nowfun=lambda: make_aware(now())
            # nowfun=dt.datetime.now
        )

    @classmethod
    def from_schedule(cls, session, schedule, period=SECONDS):
        every = max(schedule.run_every.total_seconds(), 0)
        model = session.query(IntervalSchedule).filter_by(
            every=every, period=period).first()
        if not model:
            model = cls(every=every, period=period)
            session.add(model)
            session.commit()
        return model

    @property
    def period_singular(self):
        return self.period[:-1]


class CrontabSchedule(ModelBase, ModelMixin):
    __tablename__ = 'celery_crontab_schedule'
    __table_args__ = {
        'sqlite_autoincrement': True,
        'schema': 'celery_schema'
    }

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    minute = sa.Column(sa.String(60 * 4), default='*')
    hour = sa.Column(sa.String(24 * 4), default='*')
    day_of_week = sa.Column(sa.String(64), default='*')
    day_of_month = sa.Column(sa.String(31 * 4), default='*')
    month_of_year = sa.Column(sa.String(64), default='*')
    timezone = sa.Column(sa.String(64), default='UTC')

    def __repr__(self):
        return '{0} {1} {2} {3} {4} (m/h/d/dM/MY) {5}'.format(
            cronexp(self.minute), cronexp(self.hour),
            cronexp(self.day_of_week), cronexp(self.day_of_month),
            cronexp(self.month_of_year), str(self.timezone)
        )

    @property
    def schedule(self):
        return TzAwareCrontab(
            minute=self.minute,
            hour=self.hour, day_of_week=self.day_of_week,
            day_of_month=self.day_of_month,
            month_of_year=self.month_of_year,
            tz=ZoneInfo(self.timezone)
        )

    @classmethod
    def from_schedule(cls, session, schedule):
        spec = {
            'minute': schedule._orig_minute,
            'hour': schedule._orig_hour,
            'day_of_week': schedule._orig_day_of_week,
            'day_of_month': schedule._orig_day_of_month,
            'month_of_year': schedule._orig_month_of_year,
        }
        if schedule.tz:
            spec.update({
                'timezone': schedule.tz.key
            })
        model = session.query(CrontabSchedule).filter_by(**spec).first()
        if not model:
            model = cls(**spec)
            session.add(model)
            session.commit()
        return model


class SolarSchedule(ModelBase, ModelMixin):
    __tablename__ = 'celery_solar_schedule'
    __table_args__ = {
        'sqlite_autoincrement': True,
        'schema': 'celery_schema'
    }

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)

    event = sa.Column(sa.String(24))
    latitude = sa.Column(sa.Float())
    longitude = sa.Column(sa.Float())

    @property
    def schedule(self):
        return schedules.solar(
            self.event,
            self.latitude,
            self.longitude,
            nowfun=dt.datetime.now
        )

    @classmethod
    def from_schedule(cls, session, schedule):
        spec = {
            'event': schedule.event,
            'latitude': schedule.lat,
            'longitude': schedule.lon
        }
        model = session.query(SolarSchedule).filter_by(**spec).first()
        if not model:
            model = cls(**spec)
            session.add(model)
            session.commit()
        return model

    def __repr__(self):
        return '{0} ({1}, {2})'.format(
            self.event,
            self.latitude,
            self.longitude
        )


class ClockedSchedule(ModelBase, ModelMixin):
    __tablename__ = 'celery_clocked_schedule'
    __table_args__ = {
        'sqlite_autoincrement': True,
        'schema': 'celery_schema'
    }

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    clocked_time = sa.Column(sa.DateTime(timezone=True))

    def __repr__(self):
        return f'{self.clocked_time}'

    @property
    def schedule(self):
        c = clocked(clocked_time=self.clocked_time)
        return c

    @classmethod
    def from_schedule(cls, session, schedule):
        spec = {'clocked_time': schedule.clocked_time}
        model = session.query(ClockedSchedule).filter_by(**spec).first()
        if not model:
            model = cls(**spec)
            session.add(model)
            session.commit()
        return model


class PeriodicTaskChanged(ModelBase, ModelMixin):
    """Helper table for tracking updates to periodic tasks."""

    __tablename__ = 'celery_periodic_task_changed'
    __table_args__ = {'schema': 'celery_schema'}

    id = sa.Column(sa.Integer, primary_key=True)
    last_update = sa.Column(
        sa.DateTime(timezone=True), nullable=False, default=dt.datetime.now)

    @classmethod
    def changed(cls, mapper, connection, target):
        """
        :param mapper: the Mapper which is the target of this event
        :param connection: the Connection being used
        :param target: the mapped instance being persisted
        """
        if not target.no_changes:
            cls.update_changed(mapper, connection, target)

    @classmethod
    def update_changed(cls, mapper, connection, target):
        """
        :param mapper: the Mapper which is the target of this event
        :param connection: the Connection being used
        :param target: the mapped instance being persisted
        """
        s = connection.execute(select(PeriodicTaskChanged).
                               where(PeriodicTaskChanged.id == 1).limit(1))
        if not s:
            s = connection.execute(insert(PeriodicTaskChanged),
                                   last_update=dt.datetime.now())
        else:
            s = connection.execute(update(PeriodicTaskChanged).
                                   where(PeriodicTaskChanged.id == 1).
                                   values(last_update=dt.datetime.now()))

    @classmethod
    def last_change(cls, session):
        periodic_tasks = session.query(PeriodicTaskChanged).get(1)
        if periodic_tasks:
            return periodic_tasks.last_update


class PeriodicTask(ModelBase, ModelMixin):

    __tablename__ = 'celery_periodic_task'
    __table_args__ = {
        'sqlite_autoincrement': True,
        'schema': 'celery_schema'
    }

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    # name
    name = sa.Column(sa.String(255), unique=True)
    # task name
    task = sa.Column(sa.String(255))

    # not use ForeignKey
    interval_id = sa.Column(sa.Integer)
    interval = relationship(
        IntervalSchedule,
        uselist=False,
        primaryjoin=foreign(interval_id) == remote(IntervalSchedule.id)
    )

    crontab_id = sa.Column(sa.Integer)
    crontab = relationship(
        CrontabSchedule,
        uselist=False,
        primaryjoin=foreign(crontab_id) == remote(CrontabSchedule.id)
    )

    solar_id = sa.Column(sa.Integer)
    solar = relationship(
        SolarSchedule,
        uselist=False,
        primaryjoin=foreign(solar_id) == remote(SolarSchedule.id)
    )

    clocked_id = sa.Column(sa.Integer)
    clocked = relationship(
        ClockedSchedule,
        uselist=False,
        primaryjoin=foreign(clocked_id) == remote(ClockedSchedule.id)
    )

    args = sa.Column(sa.Text(), default='[]')
    kwargs = sa.Column(sa.Text(), default='{}')
    # queue for celery
    queue = sa.Column(sa.String(255))
    # exchange for celery
    exchange = sa.Column(sa.String(255))
    # routing_key for celery
    routing_key = sa.Column(sa.String(255))
    priority = sa.Column(sa.Integer())
    expires = sa.Column(sa.DateTime(timezone=True))

    # 只执行一次
    one_off = sa.Column(sa.Boolean(), default=False)
    start_time = sa.Column(sa.DateTime(timezone=True))
    enabled = sa.Column(sa.Boolean(), default=True)
    last_run_at = sa.Column(sa.DateTime(timezone=True))
    total_run_count = sa.Column(sa.Integer(), nullable=False, default=0)
    # 修改时间
    date_changed = sa.Column(sa.DateTime(timezone=True),
                             default=func.now(), onupdate=func.now())
    description = sa.Column(sa.Text(), default='')

    no_changes = False

    @classmethod
    def receive_before_insert(cls, mapper, connection, target):
        schedule_types = ['interval_id', 'crontab_id', 'solar_id', 'clocked_id']
        selected_schedule_types = [s for s in schedule_types
                                   if getattr(target, s)]
        if len(selected_schedule_types) == 0:
            raise ValueError(
                'One of clocked, interval, crontab, or solar '
                'must be set.'
            )
        elif len(selected_schedule_types) > 1:
            raise ValueError('Only one of clocked, interval, crontab, '
                             'or solar must be set')
        if target.clocked_id and not target.one_off:
            raise ValueError("one_off must be True for clocked schedule")

    def __repr__(self):
        fmt = '{0.name}: {{no schedule}}'
        if self.interval:
            fmt = '{0.name}: {0.interval}'
        elif self.crontab:
            fmt = '{0.name}: {0.crontab}'
        elif self.solar:
            fmt = '{0.name}: {0.solar}'
        elif self.clocked:
            fmt = '{0.name}: {0.clocked}'
        return fmt.format(self)

    @property
    def task_name(self):
        return self.task

    @task_name.setter
    def task_name(self, value):
        self.task = value

    @property
    def schedule(self):
        if self.interval:
            return self.interval.schedule
        elif self.crontab:
            return self.crontab.schedule
        elif self.solar:
            return self.solar.schedule
        elif self.clocked:
            return self.clocked.schedule
        raise ValueError('{} schedule is None!'.format(self.name))


listen(PeriodicTask, 'after_insert', PeriodicTaskChanged.update_changed)
listen(PeriodicTask, 'after_delete', PeriodicTaskChanged.update_changed)
listen(PeriodicTask, 'after_update', PeriodicTaskChanged.changed)
listen(PeriodicTask, 'before_insert', PeriodicTask.receive_before_insert)
listen(IntervalSchedule, 'after_insert', PeriodicTaskChanged.update_changed)
listen(IntervalSchedule, 'after_delete', PeriodicTaskChanged.update_changed)
listen(IntervalSchedule, 'after_update', PeriodicTaskChanged.update_changed)
listen(CrontabSchedule, 'after_insert', PeriodicTaskChanged.update_changed)
listen(CrontabSchedule, 'after_delete', PeriodicTaskChanged.update_changed)
listen(CrontabSchedule, 'after_update', PeriodicTaskChanged.update_changed)
listen(SolarSchedule, 'after_insert', PeriodicTaskChanged.update_changed)
listen(SolarSchedule, 'after_delete', PeriodicTaskChanged.update_changed)
listen(SolarSchedule, 'after_update', PeriodicTaskChanged.update_changed)
listen(ClockedSchedule, 'after_insert', PeriodicTaskChanged.update_changed)
listen(ClockedSchedule, 'after_delete', PeriodicTaskChanged.update_changed)
listen(ClockedSchedule, 'after_update', PeriodicTaskChanged.update_changed)
