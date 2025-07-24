import random
from enum import Enum
from typing import List, Optional

import pandas
import numpy as np
from django.db import transaction
from nest.config import ExperimentConfig
from nest.helpers import memoized, my_argmin
from nest.models import Content, Experiment, QuestPlus, Round, Session, Stimulus, \
    StimulusGroup, StimulusVoteGroup, Subject, Vote


class SessionStatus(Enum):
    """
    A status of a Session could be one of the following:
    - UNINITIALIZED: Session is created but no Rounds are created and
    assigned a StimulusGroup.
    - PARTIALLY_INITIALIZED: Session is created and at least one (but not
    all) Rounds are created and assigned a StimulusGroup.
    - INITIALIZED: All Rounds are created and assigned a StimulusGroup.
    - PARTIALLY_FINISHED: Some (not all) Votes have been recorded for the
    Session.
    - FINISHED: All Votes have been recorded for the Session. This means that
    all StimulusVoteGroup associated withe the StimulusGroup of each Round
    has a Vote (foreign-keyed by that Round).

    Refer to the Data Model diagram for the relations between Round,
    StimulusGroup, StimulusVoteGroup and Vote:
    https://docs.google.com/presentation/d/1dvdsim38_p4zB_D1sIm3p8_0F9og7_VaKkDm-8g6Eps/edit#slide=id.ge6986ddd86_1_79
    """
    UNINITIALIZED = 1
    PARTIALLY_INITIALIZED = 2
    INITIALIZED = 3
    PARTIALLY_FINISHED = 4
    FINISHED = 5


class ExperimentController(object):
    """Controller of the business logic of an Experiment."""

    def __init__(self, experiment: Experiment,
                 experiment_config: ExperimentConfig,
                 ) -> None:
        self.experiment: Experiment = experiment
        self.experiment_config: ExperimentConfig = experiment_config
        self.randgen = random.Random(self.experiment_config.random_seed)

    def get_and_assert_current_stimulusgroups(self):
        # tools developed for testing

        # scan sg added by far, put them into dictionary of sgid as key, and
        # assert that the sgid is unique
        d_sgid_to_sg = dict()
        for s in self.experiment.session_set.all():
            for r in s.round_set.all():
                if r.stimulusgroup is not None and r.stimulusgroup.stimulusgroup_id is not None:
                    if r.stimulusgroup.stimulusgroup_id in d_sgid_to_sg:
                        assert d_sgid_to_sg[r.stimulusgroup.stimulusgroup_id] == r.stimulusgroup, \
                            'expect unique stimulusgroup with sgid {} but have at least two: {}, {}'.\
                            format(r.stimulusgroup.stimulusgroup_id,
                                   d_sgid_to_sg[r.stimulusgroup.stimulusgroup_id],
                                   r.stimulusgroup)
                    else:
                        d_sgid_to_sg[r.stimulusgroup.stimulusgroup_id] = \
                            r.stimulusgroup

        stimulusgroups = list()
        for stimulusgroup in self.experiment_config.stimulus_config.stimulusgroups:
            stimulusgroup_id = stimulusgroup['stimulusgroup_id']
            if stimulusgroup_id in d_sgid_to_sg:
                sg = d_sgid_to_sg[stimulusgroup_id]
                stimulusgroups.append(sg)
        return stimulusgroups

    def populate_stimuli(self):
        """
        populate Content, Stimulus, StimulusVoteGroup and StimulusGroup based on config.
        """

        cd: dict
        for cd in self.experiment_config.stimulus_config.contents:
            try:
                Content.objects.get(
                    experiment=self.experiment,
                    content_id=cd['content_id'])
            except Content.DoesNotExist:
                Content.objects.create(
                    experiment=self.experiment,
                    content_id=cd['content_id'])

        sd: dict
        for sd in self.experiment_config.stimulus_config.stimuli:
            try:
                Stimulus.objects.get(
                    experiment=self.experiment,
                    stimulus_id=sd['stimulus_id'])
            except Stimulus.DoesNotExist:
                c: Content = Content.objects.get(
                    experiment=self.experiment,
                    content_id=sd['content_id'])
                Stimulus.objects.create(
                    experiment=self.experiment,
                    stimulus_id=sd['stimulus_id'],
                    content=c,
                    distortion_level=sd.get('distortion_level'),
                )  # TODO: add condition in future

        svgd: dict
        for svgd in self.experiment_config.stimulus_config.stimulusvotegroups:
            assert len(svgd['stimulus_ids']) in [1, 2], \
                'for now, can only deal with stimulus_ids of length 1 or 2, ' \
                'but got: {}'.format(len(svgd['stimulus_ids']))
            try:
                StimulusVoteGroup.objects.get(
                    experiment=self.experiment,
                    stimulusvotegroup_id=svgd['stimulusvotegroup_id'])
            except StimulusVoteGroup.DoesNotExist:
                if len(svgd['stimulus_ids']) == 1:
                    s: Stimulus = Stimulus.objects.get(
                        experiment=self.experiment,
                        stimulus_id=svgd['stimulus_ids'][0])
                    StimulusVoteGroup.create_stimulusvotegroup_from_stimulus(
                        s,
                        stimulusvotegroup_id=svgd['stimulusvotegroup_id'],
                        experiment=self.experiment)
                elif len(svgd['stimulus_ids']) == 2:
                    s1: Stimulus = Stimulus.objects.get(
                        experiment=self.experiment,
                        stimulus_id=svgd['stimulus_ids'][0])
                    s2: Stimulus = Stimulus.objects.get(
                        experiment=self.experiment,
                        stimulus_id=svgd['stimulus_ids'][1])
                    StimulusVoteGroup.create_stimulusvotegroup_from_stimuli_pair(
                        s1, s2,
                        stimulusvotegroup_id=svgd['stimulusvotegroup_id'],
                        experiment=self.experiment)
                else:
                    assert False

        sgd: dict
        for sgd in self.experiment_config.stimulus_config.stimulusgroups:
            assert len(sgd['stimulusvotegroup_ids']) > 0
            try:
                StimulusGroup.objects.get(
                    experiment=self.experiment,
                    stimulusgroup_id=sgd['stimulusgroup_id'])
            except StimulusGroup.DoesNotExist:
                sg = StimulusGroup.objects.create(
                    experiment=self.experiment,
                    stimulusgroup_id=sgd['stimulusgroup_id'])
                for svgid in sgd['stimulusvotegroup_ids']:
                    svg = StimulusVoteGroup.objects.get(
                        experiment=self.experiment,
                        stimulusvotegroup_id=svgid)
                    assert svg.stimulusgroup is None, \
                        'svg.stimulusgroup should not be overwritten'
                    svg.stimulusgroup = sg
                    svg.save()

        # Create QuestPlus instances if playlist_logic is "questplus"
        if self.experiment_config.playlist_logic == 'questplus':
            self._create_questplus_instances()

    def _create_questplus_instances(self):
        """
        Create QuestPlus instances for each content when playlist_logic is 'questplus'.
        """        
        for cd in self.experiment_config.stimulus_config.contents:
            content_id = cd['content_id']
            
            try:
                content = Content.objects.get(
                    experiment=self.experiment,
                    content_id=content_id
                )
                
                # Check if QuestPlus already exists for this content
                if not hasattr(content, 'questplus') or content.questplus is None:
                    # Get QuestPlus configuration for this content
                    quest_config = self._get_questplus_config_for_content(content_id)
                    
                    if quest_config:
                        # Create QuestPlus instance
                        questplus = content.create_questplus(
                            quest_config=quest_config, 
                            experiment=self.experiment
                        )
                    else:
                        print(f"Warning: No QuestPlus config found for content {content_id}")
                else:
                    print(f"QuestPlus already exists for content {content_id}")
                    
            except Content.DoesNotExist:
                print(f"Error: Content with id {content_id} not found")

    def _get_questplus_config_for_content(self, content_id: int) -> dict:
        """
        Get QuestPlus configuration for a specific content ID.
        
        Args:
            content_id: The content ID to get configuration for
            
        Returns:
            dict: QuestPlus configuration or default config if not specified
        """
        questplus_configs = self.experiment_config.questplus_config

        if questplus_configs is not None and not isinstance(questplus_configs, dict):
            # We can either provide a specific config for a content ID, or a general config for all contents
            if str(content_id) in questplus_configs:
                return questplus_configs[str(content_id)]
            elif content_id in questplus_configs:
                return questplus_configs[content_id]
            else:
                return questplus_configs
        else:
            # Return default config if no specific config is found
            return {
                'stim_domain': dict(intensity=np.linspace(1, 51, 51).tolist()),  # QP levels in linear scale
                'outcome_domain': dict(response=['correct', 'incorrect']),  # Binary outcomes for 2AFC
                'param_domain': {
                    'threshold': np.linspace(1, 51, 51).tolist(),  # QP levels in linear scale
                    'slope': np.linspace(1, 10, 19).tolist(),  # Slope range 1-10
                    'lapse_rate': np.linspace(0, 0.04, 5).tolist(),  # Lapse rate from 0 to 0.1
                    'lower_asymptote': 0.5,  # Lower asymptote from 0 to 0.1
                },
                'func': 'weibull',  # Psychometric function
                'stim_scale': 'linear',
                'param_estimation': 'mean',
                'stim_selection': 'min_entropy'
            }

    @transaction.atomic
    def add_session(self, subject: Subject):
        """
        add a new Session to Experiment, and assign to subject. A new Session
        is created. Rounds will be created just-in-time as needed.
        """
        sess = Session(experiment=self.experiment, subject=subject)
        sess.save()
        return sess

    @transaction.atomic
    def delete_session(self, session_id):
        """
        Delete a Session, and the corresponding Rounds and Votes if exist.
        """
        sess: Session = Session.objects.get(id=session_id)
        sess.delete()

    def _get_ordering_so_far(self):
        """
        retrieve ordering so far from db, in the following format:
        [
            {'subject': subject_id, 'stimulusgroups': dict_of_round_id_to_stimulusgroup_id}
            ...
        ]
        """
        ordering = []
        s: Session
        for s in self.experiment.session_set.all():
            sess_id = s.id
            od = self._get_ordering_for_session(sess_id)
            ordering.append(od)
        return ordering

    @memoized
    def _get_ordering_for_session(self, sess_id):
        s = Session.objects.get(id=sess_id)
        subject_id = s.subject.id
        sg_dict = dict()
        r: Round
        for r in s.round_set.all():
            sg = r.stimulusgroup
            sg_dict[r.round_id] = sg.stimulusgroup_id
        # With lazy initialization, not all rounds may exist yet
        # Only validate that existing round IDs are within expected range
        if sg_dict:
            max_rid = max(sg_dict.keys())
            assert max_rid < self.experiment_config.rounds_per_session, \
                'round ids must be < {}, but found: {}'.format(
                    self.experiment_config.rounds_per_session,
                    max_rid)
        od = {
            'subject': subject_id,
            'stimulusgroups': sg_dict,
        }
        return od

    @staticmethod
    def _order(  # noqa C901
            rounds_per_session: int,
            stimulusgroup_ids: List[int],
            subject_id: int,
            ordering_so_far: List[dict],
            prioritized: List[dict],
            random_seed: Optional[int],
            blocklist_stimulusgroup_ids: List[int],
            super_stimulusgroup_ids: Optional[List[int]] = None,
    ):
        """
        return new stimulusgroup assignment in the format of:
        dict: round_id -> stimulusgroup_id

        super_stimulusgroup_ids, if present, allows for a hierarchical grouping
        of stimulusgroups. The randomization of stimulusgroups within a session
        will then be hierarchical instead of flat: first randomize between
        super_stimulusgroups, then randomize within each super_stimulusgroup.
        """

        if super_stimulusgroup_ids is not None:
            assert len(super_stimulusgroup_ids) == len(stimulusgroup_ids)
            sgid_to_super = dict(zip(stimulusgroup_ids, super_stimulusgroup_ids))
        else:
            sgid_to_super = dict()

        SUBJECT_WEIGHT = 5

        num_sessions_sofar = len(ordering_so_far)
        new_session_id = num_sessions_sofar

        # Note that stimulusgroup_idx is different from stimulusgroup_id. The
        # former has to take a value from [0, len(stimulusgroups) - 1], and the
        # latter could take value from any non-negative integers.
        d_sgid_to_sgidx = dict(zip(stimulusgroup_ids, range(len(stimulusgroup_ids))))
        d_sgidx_to_sgid = dict(zip(range(len(stimulusgroup_ids)), stimulusgroup_ids))

        # construct weight histogram:
        # use the heuristic rule to prioritize sg to test based on 5 * x + y,
        # where x is the current subject's count, and y is the overall count
        wt_hist = [0 for _ in stimulusgroup_ids]
        for pd in ordering_so_far:
            for sgid in pd['stimulusgroups'].values():
                wt_hist[d_sgid_to_sgidx[sgid]] += 1
                if subject_id == pd['subject']:
                    wt_hist[d_sgid_to_sgidx[sgid]] += SUBJECT_WEIGHT

        # output:
        d_rid_to_sgid = dict()  # output: round determined
        l_sgid = list()  # output: round not determined yet

        # fill in positions specified in prioritized list:
        for pd in prioritized:
            if (pd['session_idx'] is None  # every session needs to be applied
                    or pd['session_idx'] == new_session_id):
                sgid = pd['stimulusgroup_id']
                rid = pd['round_id']
                if rid is None:
                    l_sgid.append(sgid)
                else:
                    d_rid_to_sgid[rid] = sgid
        for rid, sgid in d_rid_to_sgid.items():
            # since it is the same subject, the weight increment is the
            # 5 * x + y, where x = 1 and y = 1
            wt_hist[d_sgid_to_sgidx[sgid]] += SUBJECT_WEIGHT + 1
        for sgid in l_sgid:
            wt_hist[d_sgid_to_sgidx[sgid]] += SUBJECT_WEIGHT + 1

        random.seed(random_seed)

        remaining_rounds_to_fulfill = rounds_per_session - len(d_rid_to_sgid) - len(l_sgid)

        sgidx_candidates = list()
        for _ in range(remaining_rounds_to_fulfill):

            # replenish if current sg candidates are empty
            if len(sgidx_candidates) == 0:
                new_sgidxs = list(range(len(stimulusgroup_ids)))
                if blocklist_stimulusgroup_ids is not None:
                    for bsgid in blocklist_stimulusgroup_ids:
                        new_sgidxs.remove(d_sgid_to_sgidx[bsgid])
                sgidx_candidates += new_sgidxs

            # find from sg candidates that has least weight
            sgidxs = my_argmin(wt_hist, sgidx_candidates)
            assert len(sgidxs) > 0
            if len(sgidxs) == 1:
                sgidx = sgidxs[0]
            else:
                sgidx = sgidxs[random.randint(0, len(sgidxs) - 1)]
            l_sgid.append(d_sgidx_to_sgid[sgidx])
            # since it is the same subject, the weight increment is the
            # 5 * x + y, where x = 1 and y = 1
            wt_hist[sgidx] += SUBJECT_WEIGHT + 1
            sgidx_candidates.remove(sgidx)

        # lastly, randomly assign sgidx in l to different rid
        if super_stimulusgroup_ids is None:
            random.shuffle(l_sgid)
        else:
            # first, randomize the order of list(set(super_stimulusgroup_ids))
            l_sgid_new = list()
            unique_super_sgids = list(set(super_stimulusgroup_ids))
            random.shuffle(unique_super_sgids)
            for super_sgid in unique_super_sgids:
                # second, randomize within each super_sgid
                cur_l = list()
                for sgid in l_sgid:
                    if sgid_to_super[sgid] == super_sgid:
                        cur_l.append(sgid)
                random.shuffle(cur_l)
                l_sgid_new += cur_l
            l_sgid = l_sgid_new

        for rid in range(rounds_per_session):
            if rid in d_rid_to_sgid:
                continue
            d_rid_to_sgid[rid] = l_sgid[0]
            del l_sgid[0]
        assert len(l_sgid) == 0

        return d_rid_to_sgid

    def get_next_stimulusgroup_id(self, session: Session, round_id: int) -> int:
        """
        Determine the next stimulus group ID for the given round using either
        QuestPlus adaptive selection or standard ordering logic.
        """
        from .models import Session
        
        # Check if we should use QuestPlus methodology
        if self.experiment_config.playlist_logic == 'questplus':
            return self._get_next_stimulusgroup_id_questplus(session, round_id)
        else:
            return self._get_next_stimulusgroup_id_standard(session, round_id)
    
    def _get_next_stimulusgroup_id_questplus(self, session: Session, round_id: int) -> int:
        """
        QuestPlus methodology: Select content that has not been viewed or is least viewed by this user,
        then use QuestPlus to estimate the next threshold for that content.
        """
        from .models import Content, StimulusGroup, Vote, Round
        from collections import defaultdict
        
        # Get content view counts for this specific user
        content_view_counts = defaultdict(int)
        user_sessions = Session.objects.filter(subject=session.subject)
        
        for user_session in user_sessions:
            user_rounds = Round.objects.filter(session=user_session)
            for user_round in user_rounds:
                votes = Vote.objects.filter(round=user_round)
                for vote in votes:
                    for stimulus in vote.stimulusvotegroup.stimuli:
                        if stimulus.content:
                            content_view_counts[stimulus.content.id] += 1
        
        # Get all available content for this experiment
        available_content = Content.objects.filter(experiment=session.experiment)
        
        # Find content with minimum view count (prioritize unviewed, then least viewed)
        min_view_count = min(content_view_counts.values())
        candidate_contents = []
        
        for content in available_content:
            view_count = content_view_counts.get(content.id, 0)
            if view_count == min_view_count:
                candidate_contents.append(content)
        
        # Select content (if multiple candidates, use the first one for consistency)
        selected_content = random.choice(candidate_contents) if len(candidate_contents) > 0 else None

        if not selected_content:
            # Fallback to standard methodology if no content found
            return self._get_next_stimulusgroup_id_standard(session, round_id)
        
        # Get QuestPlus instance for selected content and estimate next threshold
        questplus_instance = selected_content.get_questplus()
        if questplus_instance:
            # Use QuestPlus to determine the optimal stimulus level for this content
            next_threshold = questplus_instance.get_next_stimulus()
            
            # Find stimulus group that corresponds to this content and threshold level
            # Use distortion_level for precise threshold matching
            stimulus_groups = StimulusGroup.objects.filter(
                experiment=session.experiment,
                stimulus__content=selected_content,
                stimulus__distortion_level=next_threshold
            ).distinct()
            
            if stimulus_groups.exists():
                return stimulus_groups.first().stimulusgroup_id
            
            # If no exact match, find closest distortion level
            closest_stimulus_groups = StimulusGroup.objects.filter(
                experiment=session.experiment,
                stimulus__content=selected_content,
                stimulus__distortion_level__isnull=False
            ).distinct()
            
            if closest_stimulus_groups.exists():
                # Find the stimulus group with distortion level closest to next_threshold
                closest_sg = None
                min_distance = float('inf')
                
                for sg in closest_stimulus_groups:
                    for stimulus in sg.stimuli:
                        if (stimulus.content == selected_content and 
                            stimulus.distortion_level is not None):
                            distance = abs(stimulus.distortion_level - next_threshold)
                            if distance < min_distance:
                                min_distance = distance
                                closest_sg = sg
                
                if closest_sg:
                    return closest_sg.stimulusgroup_id
        
        # Fallback: find any stimulus group containing the selected content
        fallback_sg = StimulusGroup.objects.filter(
            experiment=session.experiment,
            stimulus__content=selected_content
        ).first()
        
        if fallback_sg:
            return fallback_sg.stimulusgroup_id
        
        # Final fallback to standard methodology
        return self._get_next_stimulusgroup_id_standard(session, round_id)
    
    def _get_next_stimulusgroup_id_standard(self, session: Session, round_id: int) -> int:
        """
        Standard methodology: Use existing ordering logic.
        """
        from .models import Session
        
        # Get ordering history for all other completed sessions
        ordering_so_far = []
        for sess in Session.objects.filter(experiment=session.experiment):
            if sess.id == session.id:
                continue
            od = self._get_ordering_for_session(sess.id)
            if od['stimulusgroups']:  # Only add if session has completed rounds
                ordering_so_far.append(od)
        
        # Add current session's completed rounds to history
        current_sg_dict = dict()
        for r in session.round_set.all():
            if r.round_id < round_id:  # Only include completed rounds
                current_sg_dict[r.round_id] = r.stimulusgroup.stimulusgroup_id
        
        if current_sg_dict:
            current_ordering = {
                'subject': session.subject.id,
                'stimulusgroups': current_sg_dict,
            }
            ordering_so_far.append(current_ordering)
        
        # Generate ordering for just this one round
        # Use a consistent seed based on session ID and round ID for reproducibility
        session_round_seed = hash((session.id, round_id)) % (2**16)
        d_rid_to_sgid = self._order(
            rounds_per_session=round_id + 1,  # Only generate up to current round
            stimulusgroup_ids=self.experiment_config.stimulus_config.stimulusgroup_ids,
            subject_id=session.subject.id,
            ordering_so_far=ordering_so_far,
            prioritized=self.experiment_config.prioritized,
            random_seed=session_round_seed,
            blocklist_stimulusgroup_ids=self.experiment_config.blocklist_stimulusgroup_ids,
            super_stimulusgroup_ids=self.experiment_config.stimulus_config.super_stimulusgroup_ids,
        )
        
        return d_rid_to_sgid[round_id]

    def get_or_create_round(self, session: Session, round_id: int):
        """
        Get existing round or create a new one with just-in-time stimulus selection.
        """
        from .models import Round, StimulusGroup
        
        try:
            # Try to get existing round
            return Round.objects.get(session=session, round_id=round_id)
        except Round.DoesNotExist:
            # Create new round with just-in-time stimulus selection
            sgid = self.get_next_stimulusgroup_id(session, round_id)
            sg: StimulusGroup = StimulusGroup.objects.get(
                stimulusgroup_id=sgid, experiment=session.experiment)
            r = Round(session=session, round_id=round_id, stimulusgroup=sg)
            r.save()
            return r

    def get_session_status(self, session: Session) -> SessionStatus:
        rounds: List[Round] = list(session.round_set.all())
        
        # If no rounds exist yet, session is initialized (ready to start)
        if not rounds:
            return SessionStatus.INITIALIZED

        # iterate through all StimulusVoteGroups. If all assgined Votes, return
        # 'FINISHED'; if none, return 'INITIALIZED'; otherwise, return
        # 'PARTIALLY_FINISHED'.
        all_true = None
        all_false = None
        for r in rounds:
            svg: StimulusVoteGroup
            for svg in r.stimulusgroup.stimulusvotegroup_set.all():
                votes = Vote.objects.filter(round=r, stimulusvotegroup=svg)
                if votes.count() == 0:
                    all_true = False
                elif votes.count() == 1:
                    all_false = False
                else:
                    assert False, \
                        "must have at most one Vote per StimulusVoteGroup" \
                        "but got {}: {}, {}".format(len(votes.count()),
                                                    svg, votes.all())
                if all_true is False and all_false is False:
                    return SessionStatus.PARTIALLY_FINISHED

        if all_true is False and all_false is None:
            return SessionStatus.INITIALIZED
        elif all_true is None and all_false is False:
            return SessionStatus.FINISHED
        elif all_true is None and all_false is None:
            # this happens when none of svg has been iterated
            return SessionStatus.INITIALIZED
        else:
            assert False

    @staticmethod
    def reset_session(session: Session):
        """reset session by deleting votes"""
        r: Round
        v: Vote
        for r in session.round_set.all():
            for v in r.vote_set.all():
                v.delete()
        return session

    def get_session_steps(self, session: Session) -> list:
        """
        return a list of steps for the session, including both regular rounds
        and additions (instruction steps and pre-/post-test surveys).
        stimulusgroup_id will be determined just-in-time.
        """
        steps = list()
        for rid in range(self.experiment_config.rounds_per_session):
            steps.append({
                'position': {
                    'round_id': rid,
                },
                'context': {
                    'round_id': rid,  # We'll determine stimulusgroup_id just-in-time
                }
            })
        additions = self.experiment_config.additions

        for i in range(len(steps) - 1):
            assert steps[i]['position']['round_id'] == \
                   steps[i + 1]['position']['round_id'] - 1

        for addition in additions:
            self._insert_addition_into_steps(addition, steps)

        return steps

    @staticmethod
    def _insert_addition_into_steps(addition, steps):
        round_id = addition['position']['round_id']
        before_or_after = addition['position']['before_or_after']
        # advance pos in steps, insert addition in the proper location
        if before_or_after == 'before':
            # Rule: find the first element with the same 'round_id' but
            # not with 'before_or_after' == 'before', insert before that
            # element.
            #
            # Examples:
            # (b0 means "element already inserted before round_id 0,
            #  a1 means "element already inserted after round_id 1)
            # b0, b0, 0, 1, 2, 3, 4
            #        ^
            #        |
            #   {'round_id': 0, 'before_or_after': 'before'}
            #
            # b0, b0, 0, a0, a0, b1, 1, a1, 2, 3, 4
            #                       ^
            #                       |
            #             {'round_id': 1, 'before_or_after': 'before'}
            #
            # raise exception if:
            # b0, b0, 0, a0, a0, b1, 1, a1, 2, 3, 4
            #                                       ^
            #                                       |
            #             {'round_id': 5, 'before_or_after': 'before'}
            #
            # raise exception if:
            #  b0, b0, 0, 1, 2, 3, 4
            # ^
            # |
            # {'round_id': -1, 'before_or_after': 'before'}

            for pos in range(len(steps)):
                if steps[pos]['position']['round_id'] < round_id or \
                        (round_id == steps[pos]['position']['round_id']
                         and 'before_or_after' in steps[pos]['position']
                         and steps[pos]['position']['before_or_after'] == 'before'):
                    continue
                else:
                    assert round_id == steps[pos]['position']['round_id']
                    steps.insert(pos, addition)
                    break
            else:
                # pos is at the tail of array
                assert False  # never expect insert at the end

        elif before_or_after == 'after':
            # Rule: find the first element with round_id + 1, insert before
            # that element; if reaching the end of the list, ensure that
            # the last element of the list has the same round_id (raise
            # exception if not), and insert at the end of the list.
            #
            # Examples:
            # b0, b0, 0, a0, 1, 2, 3, 4
            #               ^
            #               |
            #   {'round_id': 0, 'before_or_after': 'after'}
            #
            # b0, b0, 0, a0, a0, b1, 1, a1, 2, 3, 4, a4
            #                                          ^
            #                                          |
            #             {'round_id': 4, 'before_or_after': 'after'}
            #
            # raise exception if:
            # b0, b0, 0, a0, a0, b1, 1, a1, 2, 3, 4
            #                                       ^
            #                                       |
            #             {'round_id': 5, 'before_or_after': 'after'}
            #
            # raise exception if:
            # b0, b0, 0, a0, a0, b1, 1, a1, 2, 3, 4
            # ^
            # |
            # {'round_id': -1, 'before_or_after': 'after'}
            for pos in range(len(steps)):
                if steps[pos]['position']['round_id'] < round_id + 1:
                    continue
                else:
                    assert pos != 0  # never expect insert at the beginning
                    assert steps[pos]['position']['round_id'] == round_id + 1
                    steps.insert(pos, addition)
                    break
            else:
                # pos is at tail of array
                assert steps[-1]['position']['round_id'] == round_id
                steps.insert(len(steps), addition)
        else:
            assert False

    @staticmethod
    def get_session_info(s: Session) -> dict:

        info = dict()

        info['session_id'] = s.id
        info['subject'] = s.subject.get_subject_name()

        rounds = info.setdefault('rounds', [])
        r: Round
        for r in s.round_set.all():
            rd = dict()
            rd['round_id'] = r.round_id
            rd['stimulusgroup_id'] = r.stimulusgroup.stimulusgroup_id

            svgs = rd.setdefault('stimulusvotegroups', [])
            svg: StimulusVoteGroup
            for svg in r.stimulusgroup.stimulusvotegroup_set.all():
                svgd = dict()
                svgd['stimulusvotegroup_id'] = svg.stimulusvotegroup_id
                try:
                    vote = Vote.objects.get(round=r, stimulusvotegroup=svg)
                    svgd['vote'] = vote.score
                except Vote.DoesNotExist:
                    pass
                svgs.append(svgd)

            rounds.append(rd)

        return info

    def get_stimuli_info(self):
        return {
            'contents': self.experiment_config.stimulus_config.contents,
            'stimuli': self.experiment_config.stimulus_config.stimuli,
            'stimulusvotegroups': self.experiment_config.stimulus_config.stimulusvotegroups,
            'stimulusgroups': self.experiment_config.stimulus_config.stimulusgroups,
        }

    def get_experiment_info(self) -> dict:
        """
        Get experiment information. This is the most compact but not quite
        readable way of presenting info.
        """
        info = dict()

        info['title'] = self.experiment.title
        info['description'] = self.experiment.description

        # TODO: add experimenter

        sessions = info.setdefault('sessions', [])
        s: Session
        for s in self.experiment.session_set.all():
            sinfo = self.get_session_info(s)
            sessions.append(sinfo)

        info['stimuli_info'] = self.get_stimuli_info()

        return info

    @staticmethod
    def denormalize_experiment_info(experiment_info: dict) -> pandas.DataFrame:
        """
        flattern the experiment_info created from get_experiment_info() to make
        human readable pandas DataFrame style.
        """
        stimuli_info = experiment_info['stimuli_info']
        assert 'contents' in stimuli_info
        assert 'stimuli' in stimuli_info
        assert 'stimulusvotegroups' in stimuli_info

        # build dict of dict:
        dd_s = dict(zip([s['stimulus_id'] for s in stimuli_info['stimuli']], stimuli_info['stimuli']))
        dd_svg = dict(zip([s['stimulusvotegroup_id'] for s in stimuli_info['stimulusvotegroups']], stimuli_info['stimulusvotegroups']))

        sessions: list = experiment_info['sessions']

        rows = list()
        for sess_info in sessions:
            assert 'subject' in sess_info
            assert 'rounds' in sess_info
            rd: dict
            for rd in sess_info['rounds']:
                assert 'round_id' in rd
                assert 'stimulusvotegroups' in rd
                assert 'stimulusgroup_id' in rd
                svgds: list = rd['stimulusvotegroups']
                svgd: dict
                for svgd in svgds:
                    assert 'stimulusvotegroup_id' in svgd
                    d_svg: dict = dd_svg[svgd['stimulusvotegroup_id']]
                    assert 'stimulus_ids' in d_svg
                    sids = d_svg['stimulus_ids']
                    assert len(sids) in [1, 2], f'for now, only export case of one or two stimuli per stimulusvotegroup, but {sids}'
                    if len(sids) == 1:
                        sid = sids[0]
                        path = dd_s[sid]['path']
                        sid2 = None
                        path2 = None
                    elif len(sids) == 2:
                        sid = sids[0]
                        sid2 = sids[1]
                        path = dd_s[sid]['path']
                        path2 = dd_s[sid2]['path']
                    else:
                        assert False
                    row = {
                        'subject': sess_info['subject'],
                        'session_id': sess_info['session_id'],
                        'round_id': rd['round_id'],
                        'sg_id': rd['stimulusgroup_id'],
                        'svg_id': svgd['stimulusvotegroup_id'],
                        'vote': svgd['vote'] if 'vote' in svgd else None,
                        'sid': sid,
                        'path': path,
                    }
                    if sid2 is not None:
                        row['sid2'] = sid2
                        row['path2'] = path2
                    rows.append(row)
        return pandas.DataFrame(rows)
