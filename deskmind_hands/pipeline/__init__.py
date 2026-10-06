"""One step of a run as a fixed pipeline of stages, each with its inputs and outputs (deskmind#58):

    observe -> render -> ask -> decide -> policy -> act -> verify -> record

Moved out of the adapter and the loop one stage at a time, each move leaving what the planner is shown and what a run
does exactly as they were.
"""
