# ============================================================================
#  ar_motion_to_curve_ui.py  (AR Motion to Curve)
#
#  Python/Qt tool that records the world-space motion of selected controls
#  into a curve, rigs a locator to travel along it (position + baked
#  rotation), and can re-attach the original controls to that locator.
#
#  Curve creation samples every control's world-space position via
#  currentTime + xform, batched across all controls in a single timeline
#  scrub, and rotation baking is batched across all controls sharing the
#  same frame range too. Scrubbing the timeline once per control (for
#  sampling) and again once per control (for baking) was the main cost with
#  several controls loaded; this collapses that to about two passes total
#  in the common case where every control shares one range (Playback/Custom
#  modes), or one pass per distinct range group in Keyframe Range mode.
#  The curve itself is built as an EP (Edit Point) curve, so it interpolates
#  exactly through the sampled positions - matching Maya's EP Curve Tool,
#  rather than a CV curve where the points would only be control vertices.
#
#  Frame range per control can come from three places (chosen in the UI):
#    - the timeline's playback range (same range for every control)
#    - a user-entered custom range (same range for every control)
#    - each control's own keyframe range (auto-detected per control - this
#      is what fixes rigs where different controls are keyed over different
#      spans; forcing one shared range onto all of them produced degenerate,
#      near-static curves for any control that didn't span the full range)
#
#  U_Value (the attribute that drives position along the curve) has its
#  min/max set dynamically to match the curve's own actual parameter domain
#  (queried from the curve itself after rebuild), instead of assuming
#  rebuildCurve's keepRange flag lands exactly on 0-1. pathAnimation's
#  startU/endU are set from the same queried values.
#
#  Each control is processed inside its own try/except, so one bad control
#  (no keys, degenerate curve, etc.) logs a warning and is skipped instead
#  of stopping the whole batch.
#
#  Rig structure per control:
#    Mstr_Loc  - follows the curve via pathAnimation (position + tangent
#                orientation). Hidden helper (localScale 0), channels locked.
#    Sub_LocOffset_Grp - tracks Mstr_Loc's world position via a
#                parentConstraint. "Follow Path Angle" (a UI option) decides
#                whether Y rotation is allowed through that constraint:
#                  Off (default) - all rotation skipped, so the group stays
#                    world-axis-aligned no matter how the curve's tangent
#                    orientation changes. Editing the curve's shape, CVs, or
#                    U_Value afterward can't pop Sub_Loc's baked rotation,
#                    since it was never relative to the path's frame at all.
#                  On - only X and Z rotation are skipped, so the group
#                    turns to face the path's travel direction (yaw) while
#                    staying level (no pitch/roll from the curve's 3D shape).
#                All channels are locked once the constraint is set up.
#    Sub_Loc   - lives inside Sub_LocOffset_Grp (so its local position stays
#                zero), and carries U_Value plus baked rotation.
#
#  "Attach Controls to Locators" point- and orient-constrains each control
#  back to its Sub_Loc, only on the translate/rotate axes the control
#  actually has free, always with maintainOffset=False. Rotation constraint
#  is optional via a checkbox, since it can pop against a rig's own setup.
#
#  Generated locators (Mstr_Loc, Sub_Loc) are named with an "M2C_" prefix,
#  so a Locator Scale slider in the UI can find and resize exactly the
#  locators this tool created, anywhere in the scene, without touching
#  unrelated locators.
#
#  The old ClosestPointOn step (which relied on Maya's runtime command
#  leaving behind nodes named "cpConstraintIn"/"cpConstraintPos" by default)
#  is replaced with a `nearestPointOnCurve` node built and wired explicitly.
#
#  Usage:
#      import ar_motion_to_curve_ui
#      ar_motion_to_curve_ui.show_ui()
# ============================================================================

import maya.cmds as cmds

try:
    from PySide2 import QtCore, QtWidgets
    from shiboken2 import wrapInstance
except ImportError:
    from PySide6 import QtCore, QtWidgets
    from shiboken6 import wrapInstance

import maya.OpenMayaUI as omui


MASTER_GRP = "AR_Motion_to_Curve_Grp"
LOCATOR_PREFIX = "M2C_"
DEFAULT_SUB_LOC_SCALE = 5.0

RANGE_PLAYBACK = "playback"
RANGE_CUSTOM = "custom"
RANGE_KEYS = "keys"


# ----------------------------------------------------------------------------
#  core logic (kept separate from the UI so it can be called/tested headless)
# ----------------------------------------------------------------------------
def _short_name(long_name):
    """Leaf node name from a long (pipe-separated) DAG path. Namespace
    colons are kept, since generated helper nodes should live in the same
    namespace as the control they're built for."""
    return long_name.rsplit("|", 1)[-1]


def _unique_basis(ctrl, used_bases):
    """
    A short, collision-safe name to build generated node names from. Long
    DAG paths are never used directly for new node names (concatenating a
    suffix onto a full pipe-separated path produces something Maya reads as
    a broken path, not a name). If two loaded controls share the same leaf
    name, the second gets a numeric suffix and a warning.
    """
    basis = _short_name(ctrl)
    candidate = basis
    n = 1
    while candidate in used_bases:
        n += 1
        candidate = "%s_%d" % (basis, n)
    used_bases.add(candidate)
    if candidate != basis:
        cmds.warning(
            "Duplicate short name '%s' among selected controls - using '%s' for generated node names."
            % (basis, candidate)
        )
    return candidate


def _m2c_name(basis, suffix):
    """
    Builds an "M2C_"-prefixed node name for basis + suffix, keeping any
    namespace on basis intact rather than letting the prefix merge into a
    bogus namespace segment: basis "NS:ctrl" with suffix "_Sub_Loc" becomes
    "NS:M2C_ctrl_Sub_Loc", not "M2C_NS:ctrl_Sub_Loc" (which Maya would read
    as namespace "M2C_NS" - a leaf name that never actually starts with
    "M2C_", breaking any search for that prefix).
    """
    if ":" in basis:
        ns, leaf = basis.rsplit(":", 1)
        return "%s:%s%s%s" % (ns, LOCATOR_PREFIX, leaf, suffix)
    return "%s%s%s" % (LOCATOR_PREFIX, basis, suffix)


def get_control_key_range(ctrl, fallback_start, fallback_end):
    """
    Returns (start, end, found) for ctrl's own animation. found is False if
    ctrl has no keys at all, in which case (fallback_start, fallback_end) is
    returned so callers can fall back gracefully.
    """
    times = cmds.keyframe(ctrl, query=True, timeChange=True) or []
    if not times:
        return fallback_start, fallback_end, False
    return min(times), max(times), True


def resolve_range(ctrl, range_mode, custom_start, custom_end, playback_start, playback_end):
    """Resolve the (start, end) frame range to use for a single control."""
    if range_mode == RANGE_CUSTOM:
        return custom_start, custom_end
    if range_mode == RANGE_KEYS:
        start, end, found = get_control_key_range(ctrl, playback_start, playback_end)
        if not found:
            cmds.warning("'%s' has no keyframes - using playback range instead." % ctrl)
        return start, end
    return playback_start, playback_end


def _sample_all_controls(controls, ranges, increment, tick_callback):
    """
    Single timeline scrub sampling every control's world-space position
    (rotate pivot for transforms, translate as a fallback), instead of
    scrubbing the whole frame range once per control - the dominant cost
    with several controls loaded. Each frame is visited once regardless of
    how many controls there are; a control is only queried on frames inside
    its own resolved range.

    ranges: {ctrl: (start, end)}.
    Returns {ctrl: {"positions": [(x,y,z), ...], "knots": [frame, ...]}}.
    """
    scan_start = min(s for s, _ in ranges.values())
    scan_end = max(e for _, e in ranges.values())
    use_rotate_pivot = {c: cmds.nodeType(c) == "transform" for c in controls}

    samples = {c: {"positions": [], "knots": []} for c in controls}
    cur_time = cmds.currentTime(q=True)
    total_frames = int(round((scan_end - scan_start) / increment)) + 1

    t = scan_start
    frame = 0
    try:
        while t <= scan_end + 1e-6:
            cmds.currentTime(t, edit=True)
            for ctrl in controls:
                cs, ce = ranges[ctrl]
                if t < cs - 1e-6 or t > ce + 1e-6:
                    continue
                if use_rotate_pivot[ctrl]:
                    pos = cmds.xform(ctrl, q=True, a=True, ws=True, rp=True)
                else:
                    pos = cmds.xform(ctrl, q=True, a=True, ws=True, t=True)
                samples[ctrl]["positions"].append((pos[0], pos[1], pos[2]))
                samples[ctrl]["knots"].append(t)
            frame += 1
            if tick_callback and frame % 25 == 0:
                tick_callback("Sampling motion", frame, total_frames)
            t += increment
    finally:
        cmds.currentTime(cur_time, edit=True)

    return samples


def _dedupe_samples(positions, knots, tol=1e-5):
    """
    Collapses consecutive (near-)duplicate positions - e.g. from a held
    pose where the control doesn't move for several frames, or a control
    that isn't actually animated across the chosen range at all - since
    repeated points can make curve fitting (especially EP mode) fail
    outright on a degenerate/singular fit. Keeps positions and knots in
    sync (only relevant if this falls back to a CV curve below).
    """
    if not positions:
        return positions, knots
    tol2 = tol * tol
    out_pos = [positions[0]]
    out_knot = [knots[0]]
    for p, k in zip(positions[1:], knots[1:]):
        last = out_pos[-1]
        dist2 = (p[0] - last[0]) ** 2 + (p[1] - last[1]) ** 2 + (p[2] - last[2]) ** 2
        if dist2 > tol2:
            out_pos.append(p)
            out_knot.append(k)
    return out_pos, out_knot


def _build_curve_from_samples(curve_name, positions, knots):
    """
    Builds the curve as an EP (Edit Point) curve - it interpolates exactly
    through every sampled position, the same way Maya's EP Curve Tool
    works, rather than a CV curve where the points become control vertices
    that a higher-degree curve wouldn't necessarily touch.

    Consecutive duplicate positions (a held pose, or a control with no
    actual motion in the given range) are collapsed first, since repeated
    points are a common cause of EP fitting failing outright. If EP fitting
    still fails for some other reason, falls back to a CV curve through the
    same points instead of erroring out - EP is the default because it
    matches the motion more closely, but a working CV curve beats no curve.
    """
    positions, knots = _dedupe_samples(positions, knots)

    if len(positions) < 2:
        raise ValueError(
            "no motion detected in this frame range (every sampled position "
            "is the same point) - check the control is actually animated here"
        )

    if cmds.objExists(curve_name):
        cmds.delete(curve_name)

    degree = min(3, len(positions) - 1)
    try:
        return cmds.curve(degree=degree, editPoint=positions, name=curve_name)
    except Exception as exc:
        cmds.warning(
            "EP curve fit failed for '%s' (%s) - using a CV curve instead."
            % (curve_name, exc)
        )
        if cmds.objExists(curve_name):
            cmds.delete(curve_name)
        return cmds.curve(degree=1, point=positions, knot=knots, name=curve_name)


def _setup_control_rig(ctrl, basis, divis, obj_start, obj_end, crv, sub_loc_scale, follow_path_angle):
    """
    Rebuild, closest-point-on-curve rig, and Mstr_Loc / Sub_LocOffset_Grp /
    Sub_Loc chain for one control, wiring the position chain all the way
    through to Sub_Loc.U_Value driving the motion path. Does NOT bake -
    baking is batched separately across every control sharing the same
    frame range. Raises on failure.

    follow_path_angle: when True, Sub_LocOffset_Grp is allowed to inherit
    Mstr_Loc's Y rotation (turning to face the path's travel direction);
    when False, all rotation is skipped (group stays world-axis-aligned).

    Returns (sub_loc, cleanup_nodes), cleanup_nodes being
    (in_loc, pos_loc, aim_con, orient_con) - safe to delete only after the
    batched bake has run. (The Mstr_Loc -> Sub_LocOffset_Grp position
    constraint is NOT included here - it must stay live permanently so
    Sub_LocOffset_Grp keeps tracking Mstr_Loc during playback.)
    """
    path_grp_name = basis + "_path_Group"
    if cmds.objExists(path_grp_name):
        cmds.delete(path_grp_name)

    total_spans = max(1, int(round((obj_end - obj_start) / divis)))

    cmds.rebuildCurve(
        crv, ch=False, rpo=True, rt=0, end=1,
        kr=0, kcp=0, kep=1, kt=0, s=total_spans, d=3, tol=0.01,
    )
    cmds.xform(crv, centerPivots=True)

    # read the curve's REAL parameter domain after rebuild - don't assume
    # keepRange landed exactly on 0-1, just match whatever it actually is
    curve_min = cmds.getAttr(crv + ".minValue")
    curve_max = cmds.getAttr(crv + ".maxValue")
    if curve_max <= curve_min:
        raise ValueError("curve for '%s' has a degenerate parameter range" % basis)

    # closest-point-on-curve rig, native (no ClosestPointOn dependency) -----
    npoc = cmds.createNode("nearestPointOnCurve", name=basis + "_NPOC")
    cmds.connectAttr(crv + ".worldSpace[0]", npoc + ".inputCurve", force=True)

    in_loc = cmds.spaceLocator(name=basis + "_Path_Aim_loc")[0]
    pos_loc = cmds.spaceLocator(name=basis + "_Path_Pos_loc")[0]
    cmds.connectAttr(in_loc + ".translate", npoc + ".inPosition", force=True)
    cmds.connectAttr(npoc + ".position", pos_loc + ".translate", force=True)
    aim_con = cmds.parentConstraint(ctrl, in_loc, weight=1, maintainOffset=False)[0]

    # Mstr_Loc: follows the curve (position + tangent orientation) ---------
    mstr_loc = _m2c_name(basis, "_Mstr_Loc")
    if cmds.objExists(mstr_loc):
        cmds.delete(mstr_loc)
    cmds.spaceLocator(name=mstr_loc)

    # Sub_LocOffset_Grp: tracks Mstr_Loc's world position (rotation handled
    # per follow_path_angle below), so Sub_Loc's baked rotation is never
    # forced to inherit the curve's full tangent orientation -------------
    sub_offset_grp = basis + "_Sub_LocOffset_Grp"
    if cmds.objExists(sub_offset_grp):
        cmds.delete(sub_offset_grp)
    cmds.group(empty=True, name=sub_offset_grp)

    # Sub_Loc: lives inside Sub_LocOffset_Grp, so its local position stays
    # zero and its rotation is baked against a stable/known parent frame --
    sub_loc = _m2c_name(basis, "_Sub_Loc")
    if cmds.objExists(sub_loc):
        cmds.delete(sub_loc)
    cmds.spaceLocator(name=sub_loc)

    # match the control's rotation order, so the rotation baked later (and
    # any later re-attach) interpolates the same way the original animation
    # did instead of picking up gimbal differences from a mismatched order
    cmds.setAttr(sub_loc + ".rotateOrder", cmds.getAttr(ctrl + ".rotateOrder"))

    # U_Value's range matches the curve's real parameter domain, whatever
    # it turns out to be - this is what keeps the connection from clamping
    cmds.addAttr(sub_loc, longName="U_Value", attributeType="double",
                 min=curve_min, max=curve_max, defaultValue=curve_min)
    cmds.setAttr(sub_loc + ".U_Value", edit=True, keyable=True)

    cmds.parent(sub_loc, sub_offset_grp)
    cmds.group(empty=True, name=path_grp_name)
    cmds.parent(mstr_loc, path_grp_name)
    cmds.parent(sub_offset_grp, path_grp_name)
    cmds.parent(crv, path_grp_name)
    cmds.parent(path_grp_name, MASTER_GRP)

    # position-only (or position + yaw) tracking, kept live permanently
    # (not deleted after bake)
    skip_rotate = ["x", "z"] if follow_path_angle else ["x", "y", "z"]
    cmds.parentConstraint(
        mstr_loc, sub_offset_grp, maintainOffset=False,
        skipRotate=skip_rotate, weight=1,
    )
    cmds.setAttr(sub_offset_grp + ".translate", lock=True, keyable=False, channelBox=False)
    cmds.setAttr(sub_offset_grp + ".rotate", lock=True, keyable=False, channelBox=False)
    cmds.setAttr(sub_offset_grp + ".scale", lock=True, keyable=False, channelBox=False)

    moPath = cmds.pathAnimation(
        mstr_loc, curve=crv, fractionMode=False, follow=True,
        followAxis="z", upAxis="y", worldUpType="vector",
        worldUpVector=(0, 1, 0), inverseUp=False, inverseFront=False,
        bank=False, startU=curve_min, endU=curve_max,
    )
    # pathAnimation returns a plain string (the motionPath node's name), not
    # a list. Normalize defensively in case some Maya version returns a list.
    moPath_node = moPath[0] if isinstance(moPath, (list, tuple)) else moPath

    cmds.setAttr(mstr_loc + ".translate", lock=True, keyable=False, channelBox=False)
    cmds.setAttr(mstr_loc + ".rotate", lock=True, keyable=False, channelBox=False)
    cmds.setAttr(mstr_loc + ".scale", lock=True, keyable=False, channelBox=False)

    # localScale is a float3 attribute - setting all three components in one
    # compound setAttr call can silently fail to apply through Python cmds,
    # so each component is set individually here (same fix as the Locator
    # Scale slider uses).
    mstr_shape = cmds.listRelatives(mstr_loc, shapes=True)[0]
    for axis in ("X", "Y", "Z"):
        cmds.setAttr(mstr_shape + ".localScale" + axis, 0)

    sub_shape = cmds.listRelatives(sub_loc, shapes=True)[0]
    for axis in ("X", "Y", "Z"):
        cmds.setAttr(sub_shape + ".localScale" + axis, sub_loc_scale)

    cmds.connectAttr(npoc + ".parameter", sub_loc + ".U_Value", force=True)

    # connect the real position-driving chain BEFORE the rotation bake below
    cmds.connectAttr(sub_loc + ".U_Value", moPath_node + ".uValue", force=True)

    orient_con = cmds.orientConstraint(ctrl, sub_loc, weight=1)[0]

    return sub_loc, (in_loc, pos_loc, aim_con, orient_con)


def build_motion_to_curve(controls, divis=10, increment=1.0,
                           range_mode=RANGE_PLAYBACK, custom_start=None, custom_end=None,
                           sub_loc_scale=DEFAULT_SUB_LOC_SCALE, follow_path_angle=False,
                           progress_callback=None, tick_callback=None):
    """
    Full pipeline for a list of controls: sampled anim curve, rebuild,
    closest-point-on-curve rig, Mstr_Loc / Sub_LocOffset_Grp / Sub_Loc chain,
    path animation, and baking (position via U_Value, rotation via
    orientConstraint).

    range_mode selects how each control's frame range is determined:
      RANGE_PLAYBACK - timeline min/max, same for every control
      RANGE_CUSTOM   - custom_start/custom_end, same for every control
      RANGE_KEYS     - each control's own keyframe span, detected per control

    sub_loc_scale sets Sub_Loc's initial localScale at creation time.
    follow_path_angle - see _setup_control_rig.

    Sampling is batched into a single timeline scrub across all controls,
    and baking is batched per distinct frame range (one bakeResults call
    covering every control that shares that exact range) - both scrub the
    timeline once instead of once per control.

    progress_callback(index, total, label) fires at each pipeline stage and
    once per control during rig setup.
    tick_callback(label, frame_index, total_frames) fires periodically
    during the batched sampling pass.

    Returns (succeeded, failed, basis_map) - succeeded/failed are name lists
    (failed entries are (ctrl_name, error_message) pairs), basis_map maps
    each control's long name to the short, collision-safe name its
    generated nodes actually use (needed later by Attach Controls). A
    control failing does not stop the rest of the batch.
    """
    if not controls:
        cmds.warning("AR_Motion_to_Curve: no controls given.")
        return [], [], {}

    playback_start = cmds.playbackOptions(q=True, min=True)
    playback_end = cmds.playbackOptions(q=True, max=True)

    if not cmds.objExists(MASTER_GRP):
        cmds.group(empty=True, name=MASTER_GRP)

    succeeded, failed = [], []
    basis_map = {}
    used_bases = set()
    ranges = {}

    for ctrl in controls:
        if not cmds.objExists(ctrl):
            cmds.warning("AR_Motion_to_Curve: '%s' no longer exists, skipping." % ctrl)
            failed.append((ctrl, "no longer exists"))
            continue
        obj_start, obj_end = resolve_range(
            ctrl, range_mode, custom_start, custom_end, playback_start, playback_end
        )
        if obj_end <= obj_start:
            cmds.warning("AR_Motion_to_Curve: '%s' has an invalid frame range, skipping." % ctrl)
            failed.append((ctrl, "invalid frame range"))
            continue
        ranges[ctrl] = (obj_start, obj_end)
        basis_map[ctrl] = _unique_basis(ctrl, used_bases)

    valid_controls = list(ranges.keys())
    if not valid_controls:
        return succeeded, failed, basis_map

    total = len(valid_controls)

    # Legacy DG evaluation mode is faster than Parallel/Serial EM for this
    # kind of scripted per-frame currentTime scrubbing - EM's dispatch
    # overhead is built for interactive playback across many nodes, not a
    # tight scripted loop. Auto Key is also disabled in case it's on, since
    # it would otherwise add stray keys on every currentTime change.
    # Both are restored exactly as found, even if something below raises.
    try:
        prev_eval_mode = cmds.evaluationManager(query=True, mode=True)[0]
    except Exception:
        prev_eval_mode = None
    prev_autokey = cmds.autoKeyframe(query=True, state=True)

    cmds.refresh(suspend=True)
    cmds.autoKeyframe(state=False)
    if prev_eval_mode is not None:
        cmds.evaluationManager(mode="off")

    try:
        if progress_callback:
            progress_callback(0, total, "Sampling motion")
        samples = _sample_all_controls(valid_controls, ranges, increment, tick_callback)

        bake_groups = {}   # (start, end) -> [sub_loc, ...]
        cleanup_all = []   # [(in_loc, pos_loc, aim_con, orient_con), ...]

        for idx, ctrl in enumerate(valid_controls):
            if progress_callback:
                progress_callback(idx, total, ctrl)
            basis = basis_map[ctrl]
            obj_start, obj_end = ranges[ctrl]
            try:
                crv = _build_curve_from_samples(
                    basis + "_animCurve",
                    samples[ctrl]["positions"], samples[ctrl]["knots"],
                )
                sub_loc, cleanup = _setup_control_rig(
                    ctrl, basis, divis, obj_start, obj_end, crv, sub_loc_scale, follow_path_angle
                )
                bake_groups.setdefault((obj_start, obj_end), []).append(sub_loc)
                cleanup_all.append(cleanup)
                succeeded.append(ctrl)
            except Exception as exc:
                cmds.warning("AR_Motion_to_Curve: '%s' failed - %s" % (ctrl, exc))
                failed.append((ctrl, str(exc)))
                continue

        if progress_callback:
            progress_callback(total, total, "Baking")

        for (obj_start, obj_end), sub_locs in bake_groups.items():
            cmds.bakeResults(
                sub_locs, simulation=False, time=(obj_start, obj_end), sampleBy=increment,
                oversamplingRate=1, disableImplicitControl=True,
                preserveOutsideKeys=True, sparseAnimCurveBake=False,
                removeBakedAttributeFromLayer=False, removeBakedAnimFromLayer=False,
                bakeOnOverrideLayer=False, minimizeRotation=True,
                attribute=["U_Value", "rotateX", "rotateY", "rotateZ"],
            )

        for in_loc, pos_loc, aim_con, orient_con in cleanup_all:
            cmds.delete(in_loc, pos_loc, aim_con, orient_con)

    finally:
        if prev_eval_mode is not None:
            cmds.evaluationManager(mode=prev_eval_mode)
        cmds.autoKeyframe(state=prev_autokey)
        cmds.refresh(suspend=False)
        cmds.refresh(force=True)

    if progress_callback:
        progress_callback(total, total, "Done")

    return succeeded, failed, basis_map


def _skip_axes(node, attr_prefix):
    """
    Which of X/Y/Z to skip when constraining attr_prefix (translate/rotate)
    on node - an axis is skipped if it doesn't exist or isn't settable
    (locked, or already has an incoming connection). This lets a control
    with only a subset of channels free (e.g. only translateX, or only
    rotateZ, in any combination) get constrained on exactly what it has.
    """
    skip = []
    for axis in ("x", "y", "z"):
        attr = attr_prefix + axis.upper()
        if not cmds.attributeQuery(attr, node=node, exists=True):
            skip.append(axis)
            continue
        if not cmds.getAttr(node + "." + attr, settable=True):
            skip.append(axis)
    return skip


def attach_controls_to_locators(controls, basis_map=None, constrain_rotation=True):
    """
    Point- (and optionally orient-) constrains each control back to its
    "M2C_<basis>_Sub_Loc" locator, always with maintainOffset=False - with
    an offset, the control would flip to wherever it was sitting when the
    constraint was made instead of snapping onto the recorded motion.
    Only the translate/rotate axes the control actually has free (unlocked,
    unconnected) are constrained, in whatever combination that turns out to
    be - a control with only translateX and rotateZ free gets exactly that.

    constrain_rotation=False skips the orient constraint entirely (position
    only), for rigs where the rotation constraint introduces pops from the
    rig's own setup - e.g. while adjusting the curve shape, moving CVs, or
    scrubbing U_Value.

    basis_map should be the mapping returned by build_motion_to_curve; if
    not supplied (e.g. attaching in a fresh session against a rig built
    earlier), each control's own short leaf name is used instead.
    Returns (attached, missing) name lists.
    """
    basis_map = basis_map or {}
    attached, missing = [], []
    for ctrl in controls:
        basis = basis_map.get(ctrl) or _short_name(ctrl)
        sub_loc = _m2c_name(basis, "_Sub_Loc")
        if not cmds.objExists(ctrl) or not cmds.objExists(sub_loc):
            missing.append(ctrl)
            continue

        skip_t = _skip_axes(ctrl, "translate")
        skip_r = _skip_axes(ctrl, "rotate") if constrain_rotation else ["x", "y", "z"]

        if len(skip_t) == 3 and len(skip_r) == 3:
            cmds.warning("'%s' has no free translate or rotate channels - nothing to constrain." % ctrl)
            missing.append(ctrl)
            continue

        if len(skip_t) < 3:
            kwargs = {"skip": skip_t} if skip_t else {}
            cmds.pointConstraint(sub_loc, ctrl, maintainOffset=False, **kwargs)
        if len(skip_r) < 3:
            kwargs = {"skip": skip_r} if skip_r else {}
            cmds.orientConstraint(sub_loc, ctrl, maintainOffset=False, **kwargs)

        attached.append(ctrl)
    return attached, missing


def set_locator_scale(scale):
    """
    Applies a uniform localScale to every locator shape this tool created
    (identified by the M2C_ naming prefix), wherever they currently are in
    the scene - not just ones from the most recent run. Returns how many
    locator shapes were updated. The wildcard leads with "*" so this still
    matches locators living inside a control's namespace (e.g.
    "NS:M2C_ctrl_Sub_LocShape"), since a plain "M2C_*" pattern only matches
    names starting with "M2C_" in the current/root namespace.

    localScale is a float3 attribute, and setting it as a single compound
    call (setAttr node.localScale x y z) without an explicit type= can fail
    silently through cmds.setAttr in Python. Setting each component
    (localScaleX/Y/Z) individually avoids that entirely.
    """
    shapes = cmds.ls("*" + LOCATOR_PREFIX + "*", type="locator") or []
    for shape in shapes:
        for axis in ("X", "Y", "Z"):
            attr = shape + ".localScale" + axis
            if cmds.getAttr(attr, settable=True):
                cmds.setAttr(attr, scale)
    return len(shapes)


# ----------------------------------------------------------------------------
#  UI
# ----------------------------------------------------------------------------
def get_maya_main_window():
    ptr = omui.MQtUtil.mainWindow()
    return wrapInstance(int(ptr), QtWidgets.QWidget)


STYLE_SHEET = """
QDialog {
    background-color: #3c3c3c;
}
QGroupBox {
    border: 1px solid #555555;
    border-radius: 3px;
    margin-top: 7px;
    padding-top: 4px;
    font-weight: bold;
    color: #d8d8d8;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 6px;
    padding: 0 3px;
}
QLabel {
    color: #cccccc;
}
QRadioButton, QCheckBox {
    color: #cccccc;
    padding: 1px;
}
QPushButton {
    background-color: #5a5a5a;
    color: #f0f0f0;
    border: 1px solid #6e6e6e;
    border-radius: 3px;
    padding: 5px;
}
QPushButton:hover {
    background-color: #686868;
}
QPushButton:pressed {
    background-color: #4a4a4a;
}
QPushButton:disabled {
    background-color: #454545;
    color: #888888;
    border: 1px solid #505050;
}
QPushButton#load_btn {
    background-color: #a06a2e;
    color: white;
    font-weight: bold;
    padding: 6px;
}
QPushButton#load_btn:hover {
    background-color: #b57a38;
}
QPushButton#create_btn {
    background-color: #3d8b6e;
    color: white;
    font-weight: bold;
    padding: 7px;
}
QPushButton#create_btn:hover {
    background-color: #47a17e;
}
QPushButton#attach_btn {
    background-color: #3d6f8b;
    color: white;
    font-weight: bold;
    padding: 6px;
}
QPushButton#attach_btn:hover {
    background-color: #4880a1;
}
QListWidget, QSpinBox, QDoubleSpinBox {
    background-color: #2e2e2e;
    color: #eeeeee;
    border: 1px solid #555555;
}
QProgressBar {
    border: 1px solid #555555;
    border-radius: 2px;
    text-align: center;
    color: #eeeeee;
    background-color: #2e2e2e;
}
QProgressBar::chunk {
    background-color: #3d8b6e;
}
"""


class AR_MotionToCurveUI(QtWidgets.QDialog):

    OBJECT_NAME = "AR_MotionToCurveUI"

    def __init__(self, parent=None):
        if parent is None:
            parent = get_maya_main_window()
        super(AR_MotionToCurveUI, self).__init__(parent)

        self.setObjectName(self.OBJECT_NAME)
        self.setWindowTitle("AR Motion to Curve")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Window)
        self.setMinimumWidth(320)
        self.setStyleSheet(STYLE_SHEET)

        self.controls = []
        self.basis_map = {}

        self._build_ui()
        self._connect_signals()
        self._init_range_defaults()

    # -- ui construction ----------------------------------------------------
    def _build_ui(self):
        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setSpacing(6)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # -- selection panel --
        sel_group = QtWidgets.QGroupBox("Controls")
        sel_layout = QtWidgets.QVBoxLayout(sel_group)

        self.control_list = QtWidgets.QListWidget()
        self.control_list.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.control_list.setMaximumHeight(90)
        sel_layout.addWidget(self.control_list)

        self.load_btn = QtWidgets.QPushButton("Load Selected")
        self.load_btn.setObjectName("load_btn")
        sel_layout.addWidget(self.load_btn)

        main_layout.addWidget(sel_group)

        # -- frame range panel --
        range_group = QtWidgets.QGroupBox("Frame Range")
        range_layout = QtWidgets.QVBoxLayout(range_group)

        self.range_button_group = QtWidgets.QButtonGroup(self)
        self.playback_radio = QtWidgets.QRadioButton("Playback Range")
        self.custom_radio = QtWidgets.QRadioButton("Custom Range")
        self.keys_radio = QtWidgets.QRadioButton("Per-Control Keys")
        self.playback_radio.setChecked(True)

        self.range_button_group.addButton(self.playback_radio)
        self.range_button_group.addButton(self.custom_radio)
        self.range_button_group.addButton(self.keys_radio)

        range_layout.addWidget(self.playback_radio)
        range_layout.addWidget(self.custom_radio)

        custom_row = QtWidgets.QHBoxLayout()
        custom_row.addSpacing(18)
        custom_row.addWidget(QtWidgets.QLabel("Start"))
        self.custom_start_spin = QtWidgets.QDoubleSpinBox()
        self.custom_start_spin.setRange(-100000, 100000)
        custom_row.addWidget(self.custom_start_spin)
        custom_row.addWidget(QtWidgets.QLabel("End"))
        self.custom_end_spin = QtWidgets.QDoubleSpinBox()
        self.custom_end_spin.setRange(-100000, 100000)
        custom_row.addWidget(self.custom_end_spin)
        range_layout.addLayout(custom_row)

        range_layout.addWidget(self.keys_radio)

        main_layout.addWidget(range_group)

        # -- options panel (divisions, increment, locator scale, follow angle) --
        opt_group = QtWidgets.QGroupBox("Options")
        opt_layout = QtWidgets.QFormLayout(opt_group)

        self.divis_spin = QtWidgets.QSpinBox()
        self.divis_spin.setMinimum(1)
        self.divis_spin.setMaximum(1000)
        self.divis_spin.setValue(3)
        opt_layout.addRow("Divisions", self.divis_spin)

        self.increment_spin = QtWidgets.QDoubleSpinBox()
        self.increment_spin.setMinimum(0.1)
        self.increment_spin.setMaximum(10.0)
        self.increment_spin.setSingleStep(0.5)
        self.increment_spin.setValue(1.0)
        opt_layout.addRow("Increment", self.increment_spin)

        scale_row = QtWidgets.QHBoxLayout()
        self.locator_scale_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.locator_scale_slider.setMinimum(1)      # represents 0.1
        self.locator_scale_slider.setMaximum(200)     # represents 20.0
        self.locator_scale_slider.setValue(int(DEFAULT_SUB_LOC_SCALE * 10))
        scale_row.addWidget(self.locator_scale_slider)

        self.locator_scale_spin = QtWidgets.QDoubleSpinBox()
        self.locator_scale_spin.setMinimum(0.1)
        self.locator_scale_spin.setMaximum(20.0)
        self.locator_scale_spin.setSingleStep(0.1)
        self.locator_scale_spin.setValue(DEFAULT_SUB_LOC_SCALE)
        self.locator_scale_spin.setMaximumWidth(60)
        scale_row.addWidget(self.locator_scale_spin)

        opt_layout.addRow("Locator Scale", scale_row)

        follow_row = QtWidgets.QHBoxLayout()
        self.follow_angle_button_group = QtWidgets.QButtonGroup(self)
        self.follow_angle_on_radio = QtWidgets.QRadioButton("On")
        self.follow_angle_off_radio = QtWidgets.QRadioButton("Off")
        self.follow_angle_off_radio.setChecked(True)
        self.follow_angle_button_group.addButton(self.follow_angle_on_radio)
        self.follow_angle_button_group.addButton(self.follow_angle_off_radio)
        follow_row.addWidget(self.follow_angle_on_radio)
        follow_row.addWidget(self.follow_angle_off_radio)
        opt_layout.addRow("Follow Path Angle", follow_row)

        main_layout.addWidget(opt_group)

        # -- actions --
        self.create_btn = QtWidgets.QPushButton("Create Motion Curve")
        self.create_btn.setObjectName("create_btn")
        self.create_btn.setEnabled(False)
        main_layout.addWidget(self.create_btn)

        self.rotation_constraint_checkbox = QtWidgets.QCheckBox("Include Rotation Constraint")
        self.rotation_constraint_checkbox.setChecked(True)
        main_layout.addWidget(self.rotation_constraint_checkbox)

        self.attach_btn = QtWidgets.QPushButton("Attach Controls")
        self.attach_btn.setObjectName("attach_btn")
        self.attach_btn.setEnabled(False)
        main_layout.addWidget(self.attach_btn)

        # -- progress + status --
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)

        self.status_label = QtWidgets.QLabel("Ready.")
        self.status_label.setWordWrap(True)
        main_layout.addWidget(self.status_label)

    def _connect_signals(self):
        self.load_btn.clicked.connect(self.on_load_selected)
        self.create_btn.clicked.connect(self.on_create_motion_curve)
        self.attach_btn.clicked.connect(self.on_attach_controls)
        self.custom_radio.toggled.connect(self._update_custom_range_enabled)
        self.locator_scale_slider.valueChanged.connect(self._on_locator_scale_slider_changed)
        self.locator_scale_spin.valueChanged.connect(self._on_locator_scale_spin_changed)

    def _init_range_defaults(self):
        try:
            start = cmds.playbackOptions(q=True, min=True)
            end = cmds.playbackOptions(q=True, max=True)
        except Exception:
            start, end = 1.0, 24.0
        self.custom_start_spin.setValue(start)
        self.custom_end_spin.setValue(end)
        self._update_custom_range_enabled()

    def _update_custom_range_enabled(self):
        enabled = self.custom_radio.isChecked()
        self.custom_start_spin.setEnabled(enabled)
        self.custom_end_spin.setEnabled(enabled)

    # -- helpers --------------------------------------------------------
    def _refresh_control_list(self):
        self.control_list.clear()
        for ctrl in self.controls:
            self.control_list.addItem(ctrl)

    def _set_status(self, text):
        self.status_label.setText(text)
        QtWidgets.QApplication.processEvents()

    def _progress(self, current, total, label):
        pct = int((float(current) / total) * 100) if total else 0
        self.progress_bar.setValue(pct)
        self._set_status("Processing %s (%d/%d)" % (label, current, total))

    def _tick(self, label, frame_index, total_frames):
        self._set_status("%s - frame %d/%d" % (label, frame_index, total_frames))

    def _current_range_mode(self):
        if self.custom_radio.isChecked():
            return RANGE_CUSTOM
        if self.keys_radio.isChecked():
            return RANGE_KEYS
        return RANGE_PLAYBACK

    # -- slots ------------------------------------------------------------
    def on_load_selected(self):
        sel = cmds.ls(selection=True, type="transform", long=True) or []
        if not sel:
            cmds.warning("Nothing selected.")
            return
        self.controls = sel
        self._refresh_control_list()
        self.create_btn.setEnabled(True)
        self.attach_btn.setEnabled(True)
        self._set_status("Loaded %d control(s)." % len(sel))

    def on_create_motion_curve(self):
        if not self.controls:
            self.on_load_selected()
        if not self.controls:
            return

        divis = self.divis_spin.value()
        increment = self.increment_spin.value()
        range_mode = self._current_range_mode()
        custom_start = self.custom_start_spin.value()
        custom_end = self.custom_end_spin.value()
        sub_loc_scale = self.locator_scale_spin.value()
        follow_path_angle = self.follow_angle_on_radio.isChecked()

        if range_mode == RANGE_CUSTOM and custom_end <= custom_start:
            cmds.warning("Custom range: End must be greater than Start.")
            return

        self.progress_bar.setValue(0)
        cmds.undoInfo(openChunk=True)
        try:
            succeeded, failed, basis_map = build_motion_to_curve(
                self.controls, divis=divis, increment=increment,
                range_mode=range_mode, custom_start=custom_start, custom_end=custom_end,
                sub_loc_scale=sub_loc_scale, follow_path_angle=follow_path_angle,
                progress_callback=self._progress, tick_callback=self._tick,
            )
            self.basis_map.update(basis_map)
        finally:
            cmds.undoInfo(closeChunk=True)
            self.progress_bar.setValue(100)

        msg = "Created %d curve(s)." % len(succeeded)
        if failed:
            names = ", ".join(name for name, _ in failed)
            msg += " Failed: %s (see Script Editor for details)." % names
            cmds.warning(msg)
        self._set_status(msg)

    def on_attach_controls(self):
        if not self.controls:
            cmds.warning("No stored control list. Use 'Load Selected' first.")
            return

        cmds.undoInfo(openChunk=True)
        try:
            attached, missing = attach_controls_to_locators(
                self.controls, self.basis_map,
                constrain_rotation=self.rotation_constraint_checkbox.isChecked(),
            )
        finally:
            cmds.undoInfo(closeChunk=True)

        msg = "Attached %d control(s)." % len(attached)
        if missing:
            msg += " Missing locator/control for: %s" % ", ".join(missing)
            cmds.warning(msg)
        self._set_status(msg)

    def _on_locator_scale_slider_changed(self, int_value):
        value = int_value / 10.0
        self.locator_scale_spin.blockSignals(True)
        self.locator_scale_spin.setValue(value)
        self.locator_scale_spin.blockSignals(False)
        self._apply_locator_scale(value)

    def _on_locator_scale_spin_changed(self, value):
        self.locator_scale_slider.blockSignals(True)
        self.locator_scale_slider.setValue(int(round(value * 10)))
        self.locator_scale_slider.blockSignals(False)
        self._apply_locator_scale(value)

    def _apply_locator_scale(self, value):
        count = set_locator_scale(value)
        if count:
            self._set_status("Locator scale set to %.1f on %d locator(s)." % (value, count))


def show_ui():
    global _ar_motion_to_curve_ui
    try:
        _ar_motion_to_curve_ui.close()
        _ar_motion_to_curve_ui.deleteLater()
    except Exception:
        pass

    _ar_motion_to_curve_ui = AR_MotionToCurveUI()
    _ar_motion_to_curve_ui.show()
    return _ar_motion_to_curve_ui


if __name__ == "__main__":
    show_ui()
