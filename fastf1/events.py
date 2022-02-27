"""
:mod:`fastf1.events` - Events module
====================================
"""
import collections
import datetime
import logging
import warnings

import dateutil.parser

with warnings.catch_warnings():
    warnings.filterwarnings(
        'ignore', message="Using slow pure-python SequenceMatcher"
    )
    # suppress that warning, it's confusing at best here, we don't need fast
    # sequence matching and the installation (on windows) requires some effort
    from thefuzz import fuzz

import pandas as pd

from fastf1.api import Cache
from fastf1.core import Session
import fastf1.ergast
from fastf1.utils import recursive_dict_get


_SESSION_TYPE_ABBREVIATIONS = {
    'R': 'Race',
    'Q': 'Qualifying',
    'SQ': 'Sprint Qualifying',
    'FP1': 'Practice 1',
    'FP2': 'Practice 2',
    'FP3': 'Practice 3'
}

_SCHEDULE_BASE_URL = "https://raw.githubusercontent.com/" \
                     "theOehrly/f1schedule/master/"


def get_session(year, gp, identifier=None, *, force_ergast=False, event=None):
    """Create a :class:`Session` object based on year, event name and session
    identifier.

    .. deprecated:: 2.2
        Creating :class:`Event` objects (previously
        :class:`fastf1.core.Weekend`) by not specifying an ``identifier`` has
        been deprecated. Use :func:`get_event` instead.

    .. deprecated:: 2.2
        The argument ``event`` has been replaced with ``identifier`` to adhere
        to new naming conventions.

    .. deprecated:: 2.2
        Testing sessions can no longer be created by specifying
        ``gp='testing'``. Use :func:`get_testing_session` instead. There is
        **no grace period** for this change. This will stop working immediately
        with the release of v2.2!

    To get a testing session, use :func:`get_testing_session`.

    Examples:

        Get the second free practice of the first race of 2021 by its session
        name abbreviation::

            >>> get_session(2021, 1, 'FP2')

        Get the qualifying of the 2020 Austrian Grand Prix by full session
        name::

            >>> get_session(2020, 'Austria', 'Qualifying')

        Get the 3rd session if the 5th Grand Prix in 2021::

            >>> get_session(2021, 5, 3)

    Args:
        year (int): Championship year
        gp (number or string): Name as str or round number as int. If gp is
            a string, a fuzzy match will be performed on all events and the
            closest match will be selected.
            Fuzzy matching uses country, location, name and officialName of
            each event as reference.

            Some examples that will be correctly interpreted: 'bahrain',
            'australia', 'abudabi', 'monza'.

            See :func:`get_event_by_name` for some further remarks on the
            fuzzy matching.

        identifier (str or int): may be one of

            - session name abbreviation: ``'FP1', 'FP2', 'FP3', 'Q',
              'SQ', 'R'``
            - full session name: ``'Practice 1', 'Practice 2', 'Practice 3',
              'Sprint Qualifying', 'Qualifying', 'Race'``
            - number of the session: ``1, 2, 3, 4, 5``

        force_ergast (bool): Always use data from the ergast database to
            create the event schedule

        event: deprecated; use identifier instead

    Returns:
        :class:`~fastf1.core.Session`:
    """
    if identifier and event:
        raise ValueError("The arguments 'identifier' and 'event' are "
                         "mutually exclusive!")

    if gp == 'testing':
        raise DeprecationWarning('Accessing test sessions through '
                                 '`get_session` has been deprecated!\nUse '
                                 '`get_testing_session` instead.')

    if event is not None:
        warnings.warn("The keyword argument 'event' has been deprecated and "
                      "will be removed in a future version.\n"
                      "Use 'identifier' instead.", FutureWarning)
        identifier = event

    event = get_event(year, gp, force_ergast=force_ergast)

    if identifier is None:
        warnings.warn("Getting `Event` objects (previously `Session`) through "
                      "`get_session` has been deprecated.\n"
                      "Use `fastf1.events.get_event` instead.", FutureWarning)
        return event  # TODO: remove in v2.3

    return event.get_session(identifier)


def get_testing_session(year, test_number, session_number):
    """Create a :class:`Session` object for testing sessions based on year,
    test  event number and session number.

    Args:
        year (int): Championship year
        test_number (int): Number of the testing event (usually at most two)
        session_number (int): Number of the session withing a specific testing
            event. Each testing event usually has three sessions.

    Returns:
        :class:`~fastf1.core.Session`

    .. versionadded:: 2.2
    """
    event = get_testing_event(year, test_number)
    return event.get_session(session_number)


def get_event(year, gp, *, force_ergast=False):
    """Create an :class:`Event` object for a specific season and gp.

    To get a testing event, use :func:`get_testing_event`.

    Args:
        year (int): Championship year
        gp (int or str): Name as str or round number as int. If gp is
            a string, a fuzzy match will be performed on all events and the
            closest match will be selected.
            Fuzzy matching uses country, location, name and officialName of
            each event as reference.
            Note that the round number cannot be used to get a testing event,
            as all testing event are round 0!
        force_ergast (bool): Always use data from the ergast database to
            create the event schedule

    Returns:
        :class:`Event`

    .. versionadded:: 2.2
    """
    schedule = get_event_schedule(year=year, include_testing=False,
                                  force_ergast=force_ergast)

    if type(gp) is str:
        event = schedule.get_event_by_name(gp)
    else:
        if gp == 0:
            raise ValueError("Cannot get testing event by round number!")
        event = schedule.get_event_by_round(gp)

    return event


def get_testing_event(year, test_number):
    """Create a :class:`Event` object for testing sessions based on year
    and test event number.

    Args:
        year (int): Championship year
        test_number (int): Number of the testing event (usually at most two)

    Returns:
        :class:`~fastf1.core.Session`

    .. versionadded:: 2.2
    """
    schedule = get_event_schedule(year=year)
    schedule = schedule[schedule.is_testing()]

    try:
        assert test_number >= 1
        return schedule.iloc[test_number-1]
    except (IndexError, AssertionError):
        raise ValueError(f"Test event number {test_number} does not exist")


def get_event_schedule(year, *, include_testing=True, force_ergast=False):
    """Create an :class:`EventSchedule` object for a specific season.

    Args:
        year (int): Championship year
        include_testing (bool): Include or exclude testing sessions from the
            event schedule.
        force_ergast (bool): Always use data from the ergast database to
            create the event schedule

    Returns:
        :class:`EventSchedule`

    .. versionadded:: 2.2
    """
    if ((year not in range(2018, datetime.datetime.now().year+1))
            or force_ergast):
        schedule = _get_schedule_from_ergast(year)
    else:
        try:
            schedule = _get_schedule(year)
        except Exception as exc:
            logging.error(f"Failed to access primary schedule backend. "
                          f"Falling back to Ergast! Reason: {exc})")
            schedule = _get_schedule_from_ergast(year)

    if not include_testing:
        schedule = schedule[~schedule.is_testing()]
    return schedule


def _get_schedule(year):
    response = Cache.requests_get(
        _SCHEDULE_BASE_URL + f"schedule_{year}.json"
    )
    df = pd.read_json(response.text)
    schedule = EventSchedule(df, year=year)
    return schedule


def _get_schedule_from_ergast(year):
    # create an event schedule using data from the ergast database
    season = fastf1.ergast.fetch_season(year)
    data = collections.defaultdict(list)
    for rnd in season:
        data['round_number'].append(int(rnd.get('round')))
        data['country'].append(
            recursive_dict_get(rnd, 'Circuit', 'Location', 'country')
        )
        data['location'].append(
            recursive_dict_get(rnd, 'Circuit', 'Location', 'locality')
        )
        data['event_name'].append(rnd.get('raceName'))
        data['official_event_name'].append("")

        try:
            date = pd.to_datetime(
                f"{rnd.get('date', '')}T{rnd.get('time', '')}",
            ).tz_localize(None)
        except dateutil.parser.ParserError:
            date = pd.NaT
        data['event_date'].append(date)

        # add sessions by assuming a 'conventional' and unchanged schedule
        # only date but not time can be assumed for non race sessions,
        #   therefore .floor to daily resolution
        data['event_format'].append("conventional")
        data['session1'].append('Practice 1')
        data['session1_date'].append(date.floor('D') - pd.Timedelta(days=2))
        data['session2'].append('Practice 2')
        data['session2_date'].append(date.floor('D') - pd.Timedelta(days=2))
        data['session3'].append('Practice 3')
        data['session3_date'].append(date.floor('D') - pd.Timedelta(days=1))
        data['session4'].append('Qualifying')
        data['session4_date'].append(date.floor('D') - pd.Timedelta(days=1))
        data['session5'].append('Race')
        data['session5_date'].append(date)

    df = pd.DataFrame(data)
    schedule = EventSchedule(df, year=year)
    return schedule


class EventSchedule(pd.DataFrame):
    """This class implements a per-season event schedule.

    This class is usually not instantiated directly. You should use
    :func:`get_event_schedule` to get an event schedule for a specific
    season.

    Args:
        *args: passed on to :class:`pandas.DataFrame` superclass
        year (int): Championship year
        **kwargs: passed on to :class:`pandas.DataFrame` superclass
            (except 'columns' which is unsupported for the event schedule)

    .. versionadded:: 2.2
    """

    _COLS_TYPES = {
        'round_number': 'int64',
        'country': 'str',
        'location': 'str',
        'official_event_name': 'str',
        'event_date': 'datetime64[ns]',
        'event_name': 'str',
        'event_format': 'str',
        'session1': 'str',
        'session1_date': 'datetime64[ns]',
        'session2': 'str',
        'session2_date': 'datetime64[ns]',
        'session3': 'str',
        'session3_date': 'datetime64[ns]',
        'session4': 'str',
        'session4_date': 'datetime64[ns]',
        'session5': 'str',
        'session5_date': 'datetime64[ns]',
        'f1_api_support': 'bool'
    }

    _metadata = ['year']

    _internal_names = ['base_class_view']

    def __init__(self, *args, year=0, **kwargs):
        kwargs['columns'] = list(self._COLS_TYPES)
        super().__init__(*args, **kwargs)
        self.year = year

        # apply column specific dtypes
        for col, _type in self._COLS_TYPES.items():
            self[col] = self[col].astype(_type)

    def __repr__(self):
        return self.base_class_view.__repr__()

    @property
    def _constructor(self):
        def _new(*args, **kwargs):
            return EventSchedule(*args, **kwargs).__finalize__(self)

        return _new

    @property
    def _constructor_sliced(self):
        def _new(*args, **kwargs):
            return Event(*args, **kwargs).__finalize__(self)

        return _new

    @property
    def base_class_view(self):
        """For a nicer debugging experience; can view DataFrame through
        this property in various IDEs"""
        return pd.DataFrame(self)

    def is_testing(self):
        """Return `True` or `False`, depending on whether each event is a
        testing event."""
        return self['event_format'] == 'testing'

    def get_event_by_round(self, round):
        """Get an :class:`Event` by its round number.

        Args:
            round (int): The round number
        Returns:
            :class:`Event`
        Raises:
            ValueError: The round does not exist in the event schedule
        """
        mask = self['round_number'] == round
        if not mask.any():
            raise ValueError(f"Invalid round: {round}")
        return self[mask].iloc[0]

    def get_event_by_name(self, name):
        """Get an :class:`Event` by its name.

        A fuzzy match is performed to find the event that best matches the
        given name. Fuzzy matching is performed using the country, location,
        name and officialName of each event. This is not guaranteed to return
        the correct result. You should therefore always check if the function
        actually returns the event you had wanted.

        .. warning:: You should avoid adding common words to ``name`` to avoid
            false string matches.
            For example, you should rather use "Belgium" instead of "Belgian
            Grand Prix" as ``name``.

        Args:
            name (str): The name of the event. For example,
                ``.get_event_by_name("british")`` and
                ``.get_event_by_name("silverstone")`` will both return the
                event for the British Grand Prix.
        Returns:
            :class:`Event`
        """
        def _matcher_strings(ev):
            strings = list()
            strings.append(ev['location'])
            strings.append(ev['country'])
            strings.append(ev['event_name'].replace("Grand Prix", ""))
            strings.append(ev['official_event_name']
                           .replace("FORMULA 1", "")
                           .replace(str(self.year), "")
                           .replace("GRAND PRIX", ""))
            return strings

        max_ratio = 0
        index = 0
        for i, event in self.iterrows():
            ratio = max(
                [fuzz.ratio(val.casefold(), name.casefold())
                 for val in _matcher_strings(event)]
            )
            if ratio > max_ratio:
                max_ratio = ratio
                index = i
        return self.loc[index]


class Event(pd.Series):
    """This class represents a single event (race weekend or testing event).

    Each event consists of one or multiple sessions, depending on the type
    of event and depending on the event format.

    This class is usually not instantiated directly. You should use
    :func:`get_event` or similar to get a specific event.

    Args:
          year (int): Championship year
    """
    _metadata = ['year']

    _internal_names = ['date', 'gp']

    def __init__(self, *args, year=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.year = year
        self._getattr_override = True  # TODO: remove in v2.3

    @property
    def _constructor(self):
        def _new(*args, **kwargs):
            return Event(*args, **kwargs).__finalize__(self)

        return _new

    def __getattribute__(self, name):
        # TODO: remove in v2.3
        if name == 'name' and getattr(self, '_getattr_override', False):
            if 'event_name' in self:
                warnings.warn(
                    "The `Weekend.name` property is deprecated and will be"
                    "removed in a future version.\n"
                    "Use `Event['event_name']` or `Event.event_name` instead.",
                    FutureWarning
                )
                # name may be accessed by pandas internals to, when data
                # does not exist yet
                return self['event_name']

        return super().__getattribute__(name)

    def __repr__(self):
        # don't show .name deprecation message when .name is accessed internally
        with warnings.catch_warnings():
            warnings.filterwarnings('ignore',
                                    message=r".*property is deprecated.*")
            return super().__repr__()

    @property
    def date(self):
        """Weekend race date (YYYY-MM-DD)

        This wraps ``self['event_date'].strftime('%Y-%m-%d')``

        .. deprecated:: 2.2
            use :attr:`Event.event_date` or :attr:`Event['event_date']` and
            use :func:`datetime.datetime.strftime` to format the desired
            string representation of the datetime object
        """
        warnings.warn("The `Weekend.date` property is deprecated and will be"
                      "removed in a future version.\n"
                      "Use `Event['event_date']` or `Event.event_date` instead.",
                      FutureWarning)
        return self['event_date'].strftime('%Y-%m-%d')

    @property
    def gp(self):
        """Weekend round number

        .. deprecated:: 2.2
            use :attr:`Event.eventNumber` or :attr:`Event['eventNumber']`
        """
        warnings.warn("The `Weekend.gp` property is deprecated and will be"
                      "removed in a future version.\n"
                      "Use `Event['round_number']` or `Event.round_number` "
                      "instead.", FutureWarning)
        return self['round_number']

    def is_testing(self):
        """Return `True` or `False`, depending on whether this event is a
        testing event."""
        return self['event_format'] == 'testing'

    def get_session_name(self, identifier):
        """Return the full session name of a specific session from this event.

        Examples:

            >>> event = get_event(2021, 1)
            >>> event.get_session_name(3)
            'Practice 3'
            >>> event.get_session_name('Q')
            'Qualifying'
            >>> event.get_session_name('praCtice 1')
            'Practice 1'

        Args:
            identifier (str or int): may be one of

                - session name abbreviation: ``'FP1', 'FP2', 'FP3', 'Q',
                  'SQ', 'R'``
                - full session name: ``'Practice 1', 'Practice 2',
                  'Practice 3', 'Sprint Qualifying', 'Qualifying', 'Race'``,
                  provided names will be normalized, so that the name is
                  case-insensitive
                - number of the session: ``1, 2, 3, 4, 5``

        Returns:
            :class:`datetime.datetime`

        Raises:
            ValueError: No matching session or invalid identifier
        """
        try:
            num = float(identifier)
        except ValueError:
            # by name or abbreviation
            for name in _SESSION_TYPE_ABBREVIATIONS.values():
                if identifier.casefold() == name.casefold():
                    session_name = name
                    break
            else:
                try:
                    session_name = \
                        _SESSION_TYPE_ABBREVIATIONS[identifier.upper()]
                except KeyError:
                    raise ValueError(f"Invalid session type '{identifier}'")

            if session_name not in self.values:
                raise ValueError(f"No session of type '{identifier}' for "
                                 f"this event")
        else:
            # by number
            if (float(num).is_integer()
                    and (num := int(num)) in (1, 2, 3, 4, 5)):
                session_name = self[f'session{num}']
            else:
                raise ValueError(f"Invalid session type '{num}'")
            if not session_name:
                raise ValueError(f"Session number {num} does not "
                                 f"exist for this event")

        return session_name

    def get_session_date(self, identifier):
        """Return the date and time (if available) at which a specific session
        of this event is or was held.

        Args:
            identifier (str or int): may be one of

                - session name abbreviation: ``'FP1', 'FP2', 'FP3', 'Q',
                  'SQ', 'R'``
                - full session name: ``'Practice 1', 'Practice 2',
                  'Practice 3', 'Sprint Qualifying', 'Qualifying', 'Race'``
                - number of the session: ``1, 2, 3, 4, 5``

                see :func:`get_session_name` for more info

        Returns:
            :class:`datetime.datetime`

        Raises:
            ValueError: No matching session or invalid identifier
        """
        session_name = self.get_session_name(identifier)
        relevant_columns = self.loc[['session1', 'session2', 'session3',
                                     'session4', 'session5']]
        mask = (relevant_columns == session_name)
        if not mask.any():
            raise ValueError(f"Session type '{identifier}' does not exist "
                             f"for this event")
        else:
            _name = mask.idxmax()
            return self[f"{_name}_date"]

    def get_session(self, identifier):
        """Return a session from this event.

        Args:
            identifier (str or int): may be one of

                - session name abbreviation: ``'FP1', 'FP2', 'FP3', 'Q',
                  'SQ', 'R'``
                - full session name: ``'Practice 1', 'Practice 2', 'Practice 3',
                  'Sprint Qualifying', 'Qualifying', 'Race'``
                - number of the session: ``1, 2, 3, 4, 5``

                see :func:`get_session_name` for more info

        Returns:
            :class:`Session` instance

        Raises:
            ValueError: No matching session or invalid identifier
        """
        try:
            num = float(identifier)
        except ValueError:
            # by name or abbreviation
            session_name = self.get_session_name(identifier)
            if session_name not in self.values:
                raise ValueError(f"No session of type '{identifier}' for "
                                 f"this event")
        else:
            # by number
            if (float(num).is_integer()
                    and (num := int(num)) in (1, 2, 3, 4, 5)):
                session_name = self[f'session{num}']
            else:
                raise ValueError(f"Invalid session type '{num}'")
            if not session_name:
                raise ValueError(f"Session number {num} does not "
                                 f"exist for this event")

        return Session(event=self, session_name=session_name)

    def get_race(self):
        """Return the race session.

        Returns:
            :class:`Session` instance
        """
        return self.get_session('R')

    def get_quali(self):
        """Return the qualifying session.

        Returns:
            :class:`Session` instance
        """
        return self.get_session('Q')

    def get_sprint(self):
        """Return the sprint session.

        Returns:
            :class:`Session` instance
        """
        return self.get_session('SQ')

    def get_practice(self, number):
        """Return the specified practice session.
        Args:
            number: 1, 2 or 3 - Free practice session number
        Returns:
            :class:`Session` instance
        """
        return self.get_session(f'FP{number}')
