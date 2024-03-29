#! /usr/bin/env python3


from collections import defaultdict

import build_model
import options
import pddl_to_prolog
import pddl
import timers

PREDICATES_FILE = "predicates.txt"
STATIC_PREDICATES_FILE = "static-predicates.txt"
STATIC_ATOMS_FILE = "static-atoms.txt"

def print_atom(atom, file):
    atom_name = str(atom)
    assert atom_name.startswith("Atom ")
    print(atom_name[len("Atom "):].replace(" ", ""), file=file)

def print_negated_atom(negated_atom, file):
    atom_name = str(negated_atom)
    assert atom_name.startswith("NegatedAtom ")
    print(atom_name[len("NegatedAtom "):].replace(" ", ""), file=file)

def add_type_predicates(types):
    result = []
    for k, l in types.items():
        for obj in l:
            result.append("Atom %s(%s)" % (k, obj))
    return result


def compute_fluent_and_static_predicates(task, model):
    all_predicates = set()
    fluent_predicates = set()
    for action in task.actions:
        for effect in action.effects:
            fluent_predicates.add(effect.literal.predicate)
            all_predicates.add(effect.literal.predicate)
        if isinstance(action.precondition, pddl.Conjunction):
            for precond in action.precondition.parts:
                all_predicates.add(precond.predicate)
        else:
            assert isinstance(action.precondition, pddl.Atom)
            all_predicates.add(action.precondition.predicate)
    for axiom in task.axioms:
        fluent_predicates.add(axiom.name)
    static_predicates = all_predicates - fluent_predicates
    return fluent_predicates, static_predicates

def dump_predicates(task, model):
    fluent_predicates, _ = compute_fluent_and_static_predicates(task, model)
    predicate_name_to_predicate = dict()
    for predicate in task.predicates:
        predicate_name_to_predicate[predicate.name] = predicate
    with open(PREDICATES_FILE, "w") as f:
        for predicate_name in fluent_predicates:
            predicate = predicate_name_to_predicate[predicate_name]
            f.write(f"{predicate_name} {len(predicate.arguments)}\n")

def dump_static_predicates(task, model):
    """Dump all static predicates. """
    _, static_predicates = compute_fluent_and_static_predicates(task, model)
    predicate_name_to_predicate = dict()
    for predicate in task.predicates:
        predicate_name_to_predicate[predicate.name] = predicate
    with open(STATIC_PREDICATES_FILE, "w") as f:
        for predicate_name in sorted(static_predicates):
            predicate = predicate_name_to_predicate[predicate_name]
            f.write(f"{predicate_name} {len(predicate.arguments)}\n")
        for type in task.types:
            f.write(f"{type.name} 1\n")

def dump_static_atoms(task, model):
    """Dump all atoms belonging to static predicates.

    A predicate is static if all its groundings are static. There are predicates
    where only a subset of their groundings are static. We dump static atoms
    belonging to non-static predicates in append_static_atoms() in translate.py.
    """
    types = get_objects_by_type(task.objects, task.types)
    type_predicates = add_type_predicates(types)
    _, static_predicates = compute_fluent_and_static_predicates(task, model)
    initial_state_atoms = set(task.init)
    with open(STATIC_ATOMS_FILE, "w") as f:
        for atom in model:
            if atom.predicate in static_predicates:
                assert atom in initial_state_atoms, atom
                print_atom(atom, file=f)
        for t in type_predicates:
            print_atom(t, file=f)

def get_fluent_facts(task, model):
    fluent_predicates = set()
    for action in task.actions:
        for effect in action.effects:
            fluent_predicates.add(effect.literal.predicate)
    for axiom in task.axioms:
        fluent_predicates.add(axiom.name)
    return {fact for fact in model
            if fact.predicate in fluent_predicates}

def get_objects_by_type(typed_objects, types):
    result = defaultdict(list)
    supertypes = {}
    for type in types:
        supertypes[type.name] = type.supertype_names
    for obj in typed_objects:
        result[obj.type_name].append(obj.name)
        for type in supertypes[obj.type_name]:
            result[type].append(obj.name)
    return result

def instantiate_goal(goal, init_facts, fluent_facts):
    # With the way this module is designed, we need to "instantiate"
    # the goal to make sure we properly deal with static conditions,
    # in particular flagging unreachable negative static goals as
    # impossible. See issue1055.
    #
    # This returns None for goals that are impossible due to static
    # facts.

    # HACK! The implementation of this probably belongs into
    # pddl.condition or a similar file, not here. The `instantiate`
    # method of conditions with its slightly weird interface and the
    # existence of the `Impossible` exceptions should perhaps be
    # implementation details of `pddl`.
    result = []
    try:
        goal.instantiate({}, init_facts, fluent_facts, result)
    except pddl.conditions.Impossible:
        return None
    return result

def instantiate(task, model):
    relaxed_reachable = False
    fluent_facts = get_fluent_facts(task, model)
    init_facts = set()
    init_assignments = {}
    for element in task.init:
        if isinstance(element, pddl.Assign):
            init_assignments[element.fluent] = element.expression
        else:
            init_facts.add(element)

    type_to_objects = get_objects_by_type(task.objects, task.types)

    instantiated_actions = []
    instantiated_axioms = []
    reachable_action_parameters = defaultdict(list)
    for atom in model:
        if isinstance(atom.predicate, pddl.Action):
            action = atom.predicate
            parameters = action.parameters
            inst_parameters = atom.args[:len(parameters)]
            # Note: It's important that we use the action object
            # itself as the key in reachable_action_parameters (rather
            # than action.name) since we can have multiple different
            # actions with the same name after normalization, and we
            # want to distinguish their instantiations.
            reachable_action_parameters[action].append(inst_parameters)
            variable_mapping = {par.name: arg
                                for par, arg in zip(parameters, atom.args)}
            inst_action = action.instantiate(
                variable_mapping, init_facts, init_assignments,
                fluent_facts, type_to_objects,
                task.use_min_cost_metric)
            if inst_action:
                instantiated_actions.append(inst_action)
        elif isinstance(atom.predicate, pddl.Axiom):
            axiom = atom.predicate
            variable_mapping = {par.name: arg
                                for par, arg in zip(axiom.parameters, atom.args)}
            inst_axiom = axiom.instantiate(variable_mapping, init_facts, fluent_facts)
            if inst_axiom:
                instantiated_axioms.append(inst_axiom)
        elif atom.predicate == "@goal-reachable":
            relaxed_reachable = True

    instantiated_goal = instantiate_goal(task.goal, init_facts, fluent_facts)

    return (relaxed_reachable, fluent_facts,
            instantiated_actions, instantiated_goal,
            sorted(instantiated_axioms), reachable_action_parameters)


def explore(task):
    prog = pddl_to_prolog.translate(task)
    model = build_model.compute_model(prog)
    if options.dump_predicates:
        dump_predicates(task, model)
    if options.dump_static_predicates:
        dump_static_predicates(task, model)
    if options.dump_static_atoms:
        dump_static_atoms(task, model)
    with timers.timing("Completing instantiation"):
        return instantiate(task, model)


if __name__ == "__main__":
    import pddl_parser
    task = pddl_parser.open()
    relaxed_reachable, atoms, actions, goals, axioms, _ = explore(task)
    print("goal relaxed reachable: %s" % relaxed_reachable)
    print("%d atoms:" % len(atoms))
    for atom in atoms:
        print(" ", atom)
    print()
    print("%d actions:" % len(actions))
    for action in actions:
        action.dump()
        print()
    print("%d axioms:" % len(axioms))
    for axiom in axioms:
        axiom.dump()
        print()
    print()
    if goals is None:
        print("impossible goal")
    else:
        print("%d goals:" % len(goals))
        for literal in goals:
            literal.dump()
