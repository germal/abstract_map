import abc
import itertools
import numpy as np
import pdb
import random
import scipy.linalg as la
import scipy.integrate as ig
import sys
import time
import warnings

warnings.filterwarnings('ignore', '.*GUI is implemented')

# Abstract class compatibility across python 2 and python 3
ABC = abc.ABCMeta('ABC', (object,), {'__slots__': ()})

# Constants for the default behaviour of spatial layout
FRICTION_COEFFICIENT = 1
INTEGRATION_DT = 0.1
SAFE_DISTANCE = 0.2

STIFF_XL = 5
STIFF_L = 1
STIFF_M = 0.5
STIFF_S = 0.01

DIST_UNIT = 1
DIR_ZERO = 0


class RungeKutta45(object):

    def __init__(self, f):
        self.f = f
        self.y = []
        self.t = 0

    def set_initial_value(self, y, t):
        self.y = y
        self.t = t

    def integrate(self, t_new):
        k1 = self.f(self.t, self.y)
        k2 = self.f(self.t, self.y + INTEGRATION_DT * 0.5 * k1)
        k3 = self.f(self.t, self.y + INTEGRATION_DT * 0.5 * k2)
        k4 = self.f(self.t, self.y + INTEGRATION_DT * k3)
        self.y += (1. / 6.) * (k1 + 2 * k2 + 2 * k3 + k4) * INTEGRATION_DT
        self.t = t_new
        return self.y


class SpatialLayout(object):
    """A set of springs and masses denoting abstract ideas about space"""

    def __init__(self, log_energy=True):
        """Constructs a new empty spatial layout"""
        self._constraints = []
        self._masses = []

        self._system_changed = False
        self._bounced_last_step = False

        self._energy_log = EnergyLog() if log_energy else None
        self._post_state_change_fcn = None

        self._log = {'a': [], 'b': [], 'c': [], 'd': [], 'e': []}

        # Initialise the ode solver
        self._ode = RungeKutta45(self._stateDerivative)
        # self._ode = ig.ode(self._stateDerivative).set_integrator(
        #     'dopri5', atol=1e-5, rtol=1e-2)

    def _pullState(self):
        """Pulls the current state matrix of the system"""
        return np.concatenate(
            [np.concatenate((m.pos, m.vel)) for m in self._masses])

    def _pushState(self, y):
        """Pushes state matrix into system (obeying any safety conditions)"""
        # TODO safety conditions
        for i, m in enumerate(self._masses):
            m.pos = y[(i * 4):(i * 4 + 2)]
            m.vel = y[(i * 4 + 2):(i * 4 + 4)]

    def _pushStateSafely(self, y_a, y_b):
        """Obeys safety criteria (using old state) while pushing new state"""
        self._pushState(y_a)
        y_delta = y_b - y_a
        self._bounced_last_step = False
        for i, m in enumerate(self._masses):
            m.vel = y_b[(i * 4 + 2):(i * 4 + 4)]

        for i, m in enumerate(self._masses):
            self._stepSafely(m, y_delta[(i * 4):(i * 4 + 2)])

    def _refreshForces(self):
        """Refreshes the force value for each mass in the system"""
        for m in self._masses:
            m.acc[:] = 0
            m.applyFriction()

        for c in self._constraints:
            c.applyForce()

    def _stateDerivative(self, t, y):
        """Computes the derivative of the current state"""
        self._pushState(y)
        self._refreshForces()
        return np.concatenate(
            [np.concatenate((m.vel, m.acc)) for m in self._masses])

    def _stepSafely(self, mass, step):
        """Steps mass position, while staying a safe distance from others"""
        # Note: we don't handle stepping over a mass and its exclusion zone
        # (mainly because it doesn't matter in terms of integrator stability)
        m_unsafe = []
        m_desired = Mass("desired")
        while m_unsafe is not None:
            # Find any clashes
            m_desired.pos = mass.pos + step
            m_unsafe = next(
                (m for m in self._masses
                 if m != mass and _distance(m_desired, m) < SAFE_DISTANCE),
                None)

            # Take a safe "chunk" out of the desired step if we have a clash
            if m_unsafe is not None:
                # Get some metrics for the collision
                intersect = _firstCircleIntersect(mass.pos, m_desired.pos,
                                                  m_unsafe.pos, SAFE_DISTANCE)
                bounce_direction_m = _reflectedDirection(
                    mass.pos, intersect, m_unsafe.pos)
                bounce_direction_mu = _reflectedDirection(
                    m_unsafe.pos, intersect, mass.pos)
                bounced_position = _reflectedPosition(mass.pos, step, intersect,
                                                      bounce_direction_m)

                # Update states from the collision, and reduce the step
                mass.vel = _rotateVectorTo(mass.vel, bounce_direction_m)
                m_unsafe.vel = _rotateVectorTo(m_unsafe.vel,
                                               bounce_direction_mu)
                mass.pos = intersect
                step = bounced_position - mass.pos
                self._bounced_last_step = True

        # We now have a safe remaining step, apply it
        mass.pos += step

    def addConstraints(self, cs):
        for c in cs:
            self.addConstraint(c)

    def addConstraint(self, c):
        """Adds a constraint (and any new masses to the layout)"""
        # Force only one mass in the system with a specified name
        for i, m in enumerate(c.masses()):
            m_found = self.getMass(m.name)
            if m_found is not None:
                if i == 0:
                    c._mass_a = m_found
                elif i == 1:
                    c._mass_b = m_found
                elif i == 2:
                    c._mass_c = m_found

        # Add in the new components
        for m in c.masses():
            self.addMass(m)

        self._constraints.append(c)

        # Mark that the system state has been changed
        self.markStateChanged()

    def addMass(self, m):
        """Adds a mass to the layout (only if it is new)"""
        if m not in self._masses:
            self._masses.append(m)

            # Mark that the system state has been changed
            self.markStateChanged()

    def getMass(self, name):
        """Returns a mass with the requested name if it exists"""
        return next((m for m in self._masses if m.name == name), None)

    def initialiseState(self):
        """Initialises the state to best match provided constraints"""
        # Sort all masses and constraints into the "best" order (best is
        # defined as iteratively placing the mass that will "complete" the most
        # remaining constraints on placement)
        cs = []
        ms = []
        while self._constraints or self._masses:
            # Recompute score for each mass (number of constraints that its
            # placement will complete)
            scores = [
                sum([
                    len(list(set(c.masses()).intersection(self._masses))) == 1
                    for c in self._constraints
                ])
                for m in self._masses
            ]

            # Place the "best" unplaced mass
            m_best = self._masses[scores.index(min(scores))]
            print("Mass placed: %s" % (m_best.name))
            ms.append(m_best)
            self._masses.remove(m_best)

            # Move all constraints with all masses placed to the placed list
            c_placed = [
                c for c in self._constraints
                if len(set(c.masses()).intersection(self._masses)) == 0
            ]
            for c in c_placed:
                print("\tConstraint placed: '%s'" % (c))
                cs.append(c)
                self._constraints.remove(c)

        # Now place all of the masses in order (using the constraints to inform
        # placement)
        print('\n\n')
        for m in ms:
            print('Mass retrieved: %s' % (m.name))
            # Get a list of placement suggestions from the added constraints
            cs_complete = [
                c for c in cs if set(c.masses()).issubset(self._masses + [m])
            ]
            for c in cs_complete:
                print("\tCompletes: '%s'" % (c))

            ps = [c.placementSuggestion(m) for c in cs_complete]
            for p in ps:
                print("\tSuggestion gained (ref: %s): %s" % (p['mass'].name, p))

            # Remove empties, and merge placement suggestions by mass that the
            # suggestion is relative to
            F_MASS = lambda x: x['mass'].name  # noqa: E731
            ps = [p for p in ps if p]
            ps_merged = []
            for m_key, g in itertools.groupby(sorted(ps, key=F_MASS), F_MASS):
                g = list(g)
                rs = np.array([list(p['r']) for p in g if 'r' in p])
                ths = np.array([list(p['th']) for p in g if 'th' in p])
                merged = {'mass': g[0]['mass']}
                if rs.size > 0:
                    merged['r'] = (np.sum(np.prod(rs, 1)) / np.sum(rs[:, 1]),
                                   np.sum(rs[:, 1]))
                if ths.size > 0:
                    mean_vector = np.sum(
                        np.array([np.cos(ths[:, 0]),
                                  np.sin(ths[:, 0])]) * ths[:, 1], 1)
                    merged['th'] = (np.arctan2(mean_vector[1], mean_vector[0]),
                                    np.sum(ths[:, 1]))
                ps_merged.append(merged)

            # Get an ideal location for placement, based on the merged
            # suggestions
            ps_merged = sorted(
                ps_merged, key=lambda x: ('r' in x and 'th' in x, 'th' in x))
            placement = np.zeros((2))
            weight = 0
            for p in ps_merged:
                print("\tMerged suggestion (ref: %s): %s" % (p['mass'].name, p))
                # Get a suggested position
                if 'r' in p and 'th' in p:
                    # Suggested is simply r,th from reference position
                    suggested = p['mass'].pos + np.array([
                        p['r'][0] * np.cos(p['th'][0]),
                        p['r'][0] * np.sin(p['th'][0])
                    ])
                    w = p['r'][1] + p['th'][1]  # Not sure if should div 2...
                elif 'th' in p:
                    # Suggested is on line at angle theta from reference, with
                    # distance along line always guaranteed to be greater than
                    # 1 (suggesting close to reference is bad for system
                    # stability, & 1 is also fallback if no current placement)
                    uv = np.array([np.cos(p['th'][0]), np.sin(p['th'][0])])
                    r = np.dot(placement - p['mass'].pos, uv)
                    suggested = p['mass'].pos + (1 if r < 1 or weight == 0 else
                                                 r) * uv
                    w = p['th'][1]
                elif 'r' in p:
                    # Suggested is a distance r from the reference, in the
                    # direction of the suggested placement (direction falls
                    # back to 0 if no existing placement)
                    uv = np.array([
                        1, 0
                    ]) if weight == 0 else ((placement - p['mass'].pos) /
                                            la.norm(placement - p['mass'].pos))
                    suggested = p['mass'].pos + p['r'][0] * uv
                    w = p['r'][1]

                # Incorporate the placement suggestion, and update the weight
                print("\tPlacement suggestion (ref: %s): (%f, %f)" %
                      (p['mass'].name, suggested[0], suggested[1]))
                placement = (placement * weight + suggested * w) / (weight + w)
                weight += w

            # FINALLY, place the mass and add the constraints in
            m.pos = placement
            print("Suggesting to place %s @ (%f, %f)" % (m.name, m.pos[0],
                                                         m.pos[1]))
            self._masses.append(m)
            cs = [c for c in cs if c not in cs_complete]
            self._constraints.extend(cs_complete)

    def logEnergy(self):
        """Writes the current system energy to the enrgy log if available"""
        if self._energy_log is not None:
            self._energy_log.logEnergy(self)

    def markStateChanged(self, system_changed=True, reset=False):
        """Explicit declaration of a change of system state"""
        self._system_changed = system_changed

        if reset:
            self.resetEnergyLog()

        self.logEnergy()

        if self._post_state_change_fcn is not None:
            self._post_state_change_fcn(self)

    def step(self):
        """Performs a single iteration of the spatial layout optimisation"""
        # Handle system changes if present
        if self._system_changed:
            self._ode.set_initial_value(self._pullState(), self._ode.t)
            self._system_changed = False

        # Perform a step with the ODE integrator
        ta = time.time()
        state = np.copy(self._ode.y)
        state_next = self._ode.integrate(self._ode.t + INTEGRATION_DT)
        self._log['a'].append(self._ode.t)
        self._log['b'].append(time.time() - ta)

        # Safely apply the suggested new state
        ta = time.time()
        # self._pushState(state_next)
        self._pushStateSafely(state, state_next)
        self._log['c'].append(time.time() - ta)

        # Mark that the system state has been changed
        ta = time.time()
        self.markStateChanged(self._bounced_last_step)
        self._log['d'].append(time.time() - ta)

    def resetEnergyLog(self):
        """Resets the energy log"""
        if self._energy_log is not None:
            self._energy_log.reset()

    def randomiseState(self, window_size=5):
        """Randomises the initial state within a given window size"""
        for m in self._masses:
            m.pos[0] = (random.random() - 0.5) * window_size
            m.pos[1] = (random.random() - 0.5) * window_size
            m.vel = np.zeros_like(m.vel)
            m.acc = np.zeros_like(m.acc)

        # Mark that the system state has been changed
        self.markStateChanged(reset=True)

    def updateConstraints(self, cs):
        """Update existing constraints from a tag id (instead of adding)"""
        assert cs[0]._tag_id >= 0, "To update, tag_ids must be >= 0"
        assert all(
            c._tag_id == cs[0]._tag_id for c in cs
        ), "All constraints that are being updated must have the same tag ID"
        tag_id = cs[0]._tag_id
        self._constraints = [
            c for c in self._constraints if c._tag_id != tag_id
        ]
        self._constraints.extend(cs)

        # Mark that the system state has been changed
        self.markStateChanged()


class EnergyLog(object):
    """Log of the energy within a layout system"""

    def __init__(self):
        """Initialise the empty logs"""
        self.reset()

    def logEnergy(self, layout):
        """Logs the current energy in the spatial layout object"""
        self.t.append(layout._ode.t)
        self.kinetic.append(sum([m.totalEnergy() for m in layout._masses]))
        self.potential.append(
            sum([c.totalEnergy() for c in layout._constraints]))

    def reset(self):
        """Resets the log"""
        self.t = []
        self.kinetic = []
        self.potential = []


class _Energised(ABC):
    """Abstraction for an inhereting class to denote it contains energy"""

    @abc.abstractmethod
    def totalEnergy(self):
        pass


class MassFixed(_Energised):
    """A point-mass, that is fixed to its initial location"""

    def __init__(self, name, pos):
        """Constructs a new fixed point mass, at a requested position"""
        _Energised.__init__(self)

        self.name = name
        self._mass = 1
        self.fixed = True
        self.pos = pos
        self.vel = np.zeros((2))
        self.acc = np.zeros((2))

    def applyFriction(self):
        """Applies the friction force to the mass"""
        pass

    def totalEnergy(self):
        """Returns the kinetic energy in the moving mass"""
        return 0


class Mass(MassFixed):
    """A point-mass, representing a toponym's location in a spatial layout"""

    def __init__(self, name, pos=None, vel=None, acc=None):
        """Constructs a new point mass, with a given name"""
        MassFixed.__init__(self, name, np.zeros((2)) if pos is None else pos)

        self.fixed = False
        self.vel = np.zeros((2)) if vel is None else vel
        self.acc = np.zeros((2)) if acc is None else acc

    def applyFriction(self):
        """Applies the friction force to the mass"""
        self.acc += -FRICTION_COEFFICIENT * self.vel

    def totalEnergy(self):
        """Returns the kinetic energy in the moving mass"""
        return 0.5 * self._mass * np.sum(np.square(self.vel))


class Constraint(_Energised, ABC):
    """A spring like constraint guide for relative position of point-masses"""

    def __init__(self, tag_id=-1):
        """Constructor which gives a tag_id to link the constraint to"""
        self._tag_id = tag_id

    @abc.abstractmethod
    def __str__(self):
        """Force every subclass to implement a verbose string representation"""
        pass

    def totalEnergy(self):
        """Returns the potential energy held by the constraint"""
        return 0.5 * self._stiffness * np.square(self.displacement())

    @abc.abstractmethod
    def applyForce(self):
        """Applies the current constraint force to each attached point-mass"""
        pass

    @abc.abstractmethod
    def displacement(self):
        """Distance the spring is displaced from its natural length"""
        pass

    @abc.abstractmethod
    def length(self):
        """Returns length of the constraint (same units as natural length)"""
        pass

    @abc.abstractmethod
    def masses(self):
        """Returns a list of masses in the constraint"""
        pass

    @abc.abstractmethod
    def placementSuggestion(self, mass):
        """Returns a placement tuple suggesting where to place the mass"""
        pass


class ConstraintDistance(Constraint):
    """A constraint on the distance between two point-masses"""

    def __init__(self, mass_a, mass_b, natural_length, stiffness, tag_id=-1):
        """Constructs the specified constraint between masses"""
        super(ConstraintDistance, self).__init__(tag_id)

        self._mass_a = mass_a
        self._mass_b = mass_b
        self._natural_length = natural_length
        self._stiffness = stiffness

    def __str__(self):
        return "Constrain distance between %s & %s to %f (%f)" % (
            self._mass_a.name, self._mass_b.name, self._natural_length,
            self._stiffness)

    def applyForce(self):
        """Applies the constraint force to masses a and b"""
        force_vector = -self._stiffness * self.displacement() * _uv(
            self._mass_a, self._mass_b)

        if not self._mass_a.fixed:
            self._mass_a.acc += force_vector / self._mass_a._mass
        if not self._mass_b.fixed:
            self._mass_b.acc += -force_vector / self._mass_b._mass

    def displacement(self):
        """Distance the spring is displaced from its natural length"""
        return self.length() - self._natural_length

    def masses(self):
        """Returns the list of masses in the distance constraint"""
        return [self._mass_a, self._mass_b]

    def length(self):
        """Returns distance between position of mass a and b"""
        return _distance(self._mass_a, self._mass_b)

    def placementSuggestion(self, mass):
        """Returns a placement tuple suggesting where to place the mass"""
        if mass == self._mass_a:
            return {
                'mass': self._mass_b,
                'r': (self._natural_length, self._stiffness)
            }
        elif mass == self._mass_b:
            return {
                'mass': self._mass_a,
                'r': (self._natural_length, self._stiffness)
            }
        else:
            return {}


class ConstraintAngleGlobal(Constraint):
    """A constraint on the angle between two point-masses, in the global frame"""

    def __init__(self, mass_a, mass_b, natural_length, stiffness, tag_id=-1):
        """Constructs the specified constraint between masses"""
        super(ConstraintAngleGlobal, self).__init__(tag_id)

        self._mass_a = mass_a
        self._mass_b = mass_b
        self._natural_length = natural_length
        self._stiffness = stiffness

    def __str__(self):
        return "Constrain angle to %s from %s to %f (%f)" % (
            self._mass_a.name, self._mass_b.name, self._natural_length,
            self._stiffness)

    def applyForce(self):
        """Applies the constraint force to masses a and b"""
        force_vector = -self._stiffness * self.displacement() / _distance(
            self._mass_a, self._mass_b) * _orthog(
                _uv(self._mass_a, self._mass_b))

        if not self._mass_a.fixed:
            self._mass_a.acc += force_vector / self._mass_a._mass
        if not self._mass_b.fixed:
            self._mass_b.acc += -force_vector / self._mass_b._mass

    def displacement(self):
        """Distance the spring is displaced from its natural length"""
        return _angleWrap(self.length() - self._natural_length)

    def masses(self):
        """Returns the list of masses in the global angular constraint"""
        return [self._mass_a, self._mass_b]

    def length(self):
        """Returns angle of of mass a relative to mass b, in global frame"""
        return _angle(self._mass_a, self._mass_b)

    def placementSuggestion(self, mass):
        """Returns a placement tuple suggesting where to place the mass"""
        if mass == self._mass_a:
            return {
                'mass': self._mass_b,
                'th': (self._natural_length, self._stiffness)
            }
        elif mass == self._mass_b:
            return {
                'mass': self._mass_a,
                'th': (_angleWrap(self._natural_length + np.pi),
                       self._stiffness)
            }
        else:
            return {}


class ConstraintAngleLocal(Constraint):
    """A constraint on the angle formed by three point-masses"""

    def __init__(self,
                 mass_a,
                 mass_b,
                 mass_c,
                 natural_length,
                 stiffness,
                 tag_id=-1):
        """Constructs the specified constraint between masses"""
        super(ConstraintAngleLocal, self).__init__(tag_id)

        self._mass_a = mass_a
        self._mass_b = mass_b
        self._mass_c = mass_c
        self._natural_length = natural_length
        self._stiffness = stiffness

    def __str__(self):
        return "Constrain angle to %s from %s (relative to %s) to %f (%f)" % (
            self._mass_a.name, self._mass_b.name, self._mass_c.name,
            self._natural_length, self._stiffness)

    def applyForce(self):
        """Applies the constraint force to masses a, b, and c"""
        force_vector_a = -self._stiffness * self.displacement() / _distance(
            self._mass_a, self._mass_b) * _orthog(
                _uv(self._mass_a, self._mass_b))
        force_vector_c = -self._stiffness * self.displacement() / _distance(
            self._mass_c,
            self._mass_b) * -_orthog(_uv(self._mass_c, self._mass_b))

        acc_a = force_vector_a / self._mass_a._mass
        acc_c = force_vector_c / self._mass_c._mass
        if not self._mass_a.fixed:
            self._mass_a.acc += acc_a
        if not self._mass_b.fixed:
            self._mass_b.acc += -acc_a + -acc_c
        if not self._mass_c.fixed:
            self._mass_c.acc += acc_c

    def displacement(self):
        """Distance the spring is displaced from its natural length"""
        return _angleWrap(self.length() - self._natural_length)

    def masses(self):
        """Returns the list of masses in the local angular constraint"""
        return [self._mass_a, self._mass_b, self._mass_c]

    def length(self):
        """Returns angle of mass a, relative to vector from mass b to c"""
        return _angle(self._mass_a, self._mass_b, self._mass_c)

    def placementSuggestion(self, mass):
        """Returns a placement tuple suggesting where to place the mass"""
        if mass == self._mass_a:
            return {
                'mass':
                self._mass_b,
                'th': (_angleWrap(
                    _angle(self._mass_c, self._mass_b) + self._natural_length),
                       self._stiffness)
            }
        elif mass == self._mass_b:
            # There is no easy way to do this (the path of possible placements
            # of B follows a complex arc, which is discontinous because it is
            # present on both sides - i.e. constraint can be on left or right
            # side of |AC|). The whole optimisation process is needed for
            # overcoming problems like these. So for now, take the easy option
            # and simply place a suggestion (relative to C) for B to be at the
            # midpoint of |AC|
            r = (1 - np.absolute(self._natural_length) /
                 (2 * np.pi)) * _distance(self._mass_a, self._mass_c)
            a = -np.pi
            b = np.pi
            dummy = Mass('dummy')
            SEARCH_DEPTH = 20
            for i in range(0, SEARCH_DEPTH):
                mid = (a + b) / 2
                dummy.pos = self._mass_a.pos + r * np.array(
                    [np.cos(mid), np.sin(mid)])
                error = _angle(self._mass_a, dummy,
                               self._mass_c) - self._natural_length
                if error > 0:
                    b = mid
                else:
                    a = mid

            return {
                'mass': self._mass_a,
                'r': (r, self._stiffness / 2),
                'th': (mid, self._stiffness / 2)
            }
        elif mass == self._mass_c:
            return {
                'mass':
                self._mass_b,
                'th': (_angleWrap(
                    _angle(self._mass_a, self._mass_b) - self._natural_length),
                       self._stiffness)
            }
        else:
            return {}


def _angle(mass_a, mass_b, mass_c=None):
    """Compute the angle formed by mass a, relative to b (and optionally c) """
    v_ab = mass_a.pos - mass_b.pos
    ret = np.arctan2(v_ab[1], v_ab[0])
    if mass_c is not None:
        v_cb = mass_c.pos - mass_b.pos
        ret -= np.arctan2(v_cb[1], v_cb[0])

    return _angleWrap(ret)


def _angleWrap(angle):
    """Returns the angle, in the range of [-PI,+PI)"""
    ret = (angle + np.pi) % (2 * np.pi)
    if ret < 0:
        ret += 2 * np.pi

    return ret - np.pi


def _distance(mass_a, mass_b):
    """Computes the distance between two masses"""
    ab = mass_a.pos - mass_b.pos
    return (ab[0]**2 + ab[1]**2)**0.5


def _firstCircleIntersect(line_a, line_b, circle_center, circle_r):
    """Finds the first point that line from a to b intesecting a circle"""
    # Find coefficients for the linear equation
    disp = line_b - line_a
    use_vertical = np.abs(disp[0]) < np.abs(disp[1])
    m = disp[0] / disp[1] if use_vertical else disp[1] / disp[0]
    c = (-m * line_a[1] + line_a[0]
         if use_vertical else -m * line_a[0] + line_a[1])

    # Find coefficients for the quadratic equation, and find the roots
    quad_a = -1 - m**2
    quad_b = (-2 * m * c + 2 * m * (circle_center[0]
                                    if use_vertical else circle_center[1]) +
              2 * (circle_center[1] if use_vertical else circle_center[0]))
    quad_c = (-c**2 + circle_r**2 - circle_center[0]**2 - circle_center[1]**2 +
              2 * c * (circle_center[0] if use_vertical else circle_center[1]))
    discriminant = quad_b**2 - 4 * quad_a * quad_c
    if discriminant < 0:
        raise ValueError("Intersection discriminant < 0")
    root_1 = (-quad_b + discriminant**0.5) / (2 * quad_a)
    root_2 = (-quad_b - discriminant**0.5) / (2 * quad_a)

    # Return the intersect that is closest to line_a
    intersect_1 = np.array([(m * root_1 + c if use_vertical else root_1),
                            (root_1 if use_vertical else m * root_1 + c)])
    intersect_2 = np.array([(m * root_2 + c if use_vertical else root_2),
                            (root_2 if use_vertical else m * root_2 + c)])
    d1 = intersect_1 - line_a
    d2 = intersect_2 - line_a
    return (intersect_1
            if (d1[0]**2 + d1[1]**2)**0.5 < (d2[0]**2 + d2[1]**2)**0.5 else
            intersect_2)


def _reflectedDirection(start_point, reflect_point, reflect_origin):
    """Gets the direction of reflection from a given point"""
    # Angle is reflect_origin->reflect_point, minus the angle of incidence
    # (where angle of incidence = start->reflect_point - reflect_point->origin)
    ro = reflect_origin - reflect_point
    sr = reflect_point - start_point
    return _angleWrap(
        np.arctan2(-ro[1], -ro[0]) -
        (np.arctan2(sr[1], sr[0]) - np.arctan2(ro[1], ro[0])))


def _reflectedPosition(start_point, step, reflect_point, reflect_direction):
    """Gets the point when a step is reflected around a given point"""
    reflect_step = reflect_point - start_point
    r = ((step[0]**2 + step[1]**2)**0.5 -
         (reflect_step[0]**2 + reflect_step[1]**2)**0.5)
    return reflect_point + r * np.array(
        [np.cos(reflect_direction),
         np.sin(reflect_direction)])


def _rotateVectorTo(vector, angle):
    """Rotates a vector to a requested orientation"""
    r = (vector[0]**2 + vector[1]**2)**0.5
    return np.array([r * np.cos(angle), r * np.sin(angle)])


def _uv(mass_a, mass_b):
    """Returns the unit vector pointing to mass a, from mass b"""
    ab = mass_a.pos - mass_b.pos
    return (np.array([1, 0]).T if np.array_equal(mass_a.pos, mass_b.pos) else
            ab / (ab[0]**2 + ab[1]**2)**0.5)


def _orthog(vector):
    return np.array([-vector[1], vector[0]])
