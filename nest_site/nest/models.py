from __future__ import annotations

import json
import json_tricks
import os
from abc import ABCMeta, abstractmethod
from typing import List, Optional

from django.contrib.auth.models import User
from django.db import models, transaction
from django.utils import timezone
from polymorphic.models import PolymorphicModel

from .helpers import TypeVersionEnabled

class GenericModel(PolymorphicModel):
    """
    Abstract model that serves as the base class of other models derived.

    It has a created_date field that can be filled automatically with the the
    creation date, and a deactivate_date filed that is None by default,
    signaling that an object is active. Once deactivate_date is set to not None,
    it signals that the object is deactivated from the set date on.
    """
    create_date = models.DateTimeField('date created', default=timezone.now)
    deactivate_date = models.DateTimeField('date deactivated', null=True,
                                           default=None, blank=True)

    @property
    def is_active(self):
        """Returns if the object is currently active."""
        return (self.deactivate_date is None
                or timezone.now() < self.deactivate_date)

    def deactivate(self):
        """To deactivate the object."""
        self.deactivate_date = timezone.now()

    class Meta:
        abstract = True

    @property
    def saved(self):
        """Returns if the object is saved in database."""
        return not (self.pk is None)

    def __str__(self):
        return f"{self.__class__.__name__} {self.id}"


class Person(GenericModel):
    """Abtract class for a Person."""
    name = models.CharField('name', max_length=100, default="", blank=True)
    user: User = models.OneToOneField(User,
                                      null=True, blank=True,
                                      on_delete=models.SET_NULL)

    class Meta:
        abstract = True

    def __str__(self):
        s = super().__str__()
        disp_fields = []
        if self.name != "":
            disp_fields += [self.name]
        if self.user is not None:
            disp_fields += [str(self.user)]
        if len(disp_fields) > 0:
            s += f" ({', '.join(disp_fields)})"
        return s

    @classmethod
    def find_by_username(cls, username: str):
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            return None
        try:
            person = cls.objects.get(user=user)
        except cls.DoesNotExist:
            return None
        return person


class Subject(Person):
    """Subject of an Experiment."""

    @classmethod
    @transaction.atomic
    def create_by_username(cls, username: str):
        """
        Create a new Subject object and associate it to a User of username.
        The method will try two things: 1) if the User exists, it will
        associate the new Subject to the User; 2) if the User does not exist,
        it will try to create a new User before association.
        """
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            user = User.objects.create_user(username=username)
        assert cls.objects.filter(user=user).count() == 0
        p = cls.objects.create(user=user)
        p.save()
        return p

    def get_subject_name(self):
        subject_name = self.name
        if subject_name == "":
            if self.user is not None:
                subject_name = self.user.username
        if subject_name == "":
            subject_name = str(self.id)
        return subject_name


class Experimenter(Person):
    """Experimenter of an Experiment."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert self.user is None or (self.user is not None and self.user.is_staff)

    @classmethod
    @transaction.atomic
    def create_by_username(cls, username: str):
        """
        Create a new Experimenter object and associate it to a User of username.
        The User must be is_staff. The method will try three things: 1) if the
        User exists, and is_staff, it will associate the new Experimenter to the
        User; 2) if the User exist but not is_staff, it will try to upgrade
        the User to is_staff before association; 3) if the User does not exist,
        it will try to create a new User which is_staff, before association.
        """
        try:
            user = User.objects.get(username=username, is_staff=True)
        except User.DoesNotExist:
            try:
                user = User.objects.get(username=username)
                user.is_staff = True
                user.save()
            except User.DoesNotExist:
                user = User.objects.create_user(username=username, is_staff=True)
        assert cls.objects.filter(user=user).count() == 0
        p = cls.objects.create(user=user)
        p.save()
        return p


class Experiment(GenericModel):
    """
    Experiment, each object typically consists of multiple Sessions, each
    Session run on a Subject.
    """
    title = models.CharField('experiment title',
                             max_length=200,
                             null=False, blank=False,
                             unique=True)
    description = models.TextField('experiment description',
                                   null=True, blank=True)
    experimenters = models.ManyToManyField(Experimenter,
                                           through='ExperimentRegister',
                                           blank=True)

    def __str__(self):
        return super().__str__() + f' ({self.title})'


class ExperimentRegister(GenericModel):
    """
    Register to associate an Experiment with an Experimenter.

    In NEST, the association between Experiment and Experimenter is
    many-to-many (an Experiment can be associated with more than one
    Experimenters, and an Experimenter can register multiple Experiments),
    and ExperimentRegister is the middle class to establish their
    association.
    """

    experiment = models.ForeignKey(Experiment, on_delete=models.CASCADE)
    experimenter = models.ForeignKey(Experimenter, on_delete=models.CASCADE)


class Session(GenericModel):
    """
    An continous interval within an Experiment, associated with one Subject.

    Multiple Sessions consist of an Experiment. A session must have one
    Subject, and may consists of multiple Rounds.
    """
    experiment: Experiment = models.ForeignKey(Experiment,
                                               on_delete=models.CASCADE)
    subject: Subject = models.ForeignKey(Subject,
                                         on_delete=models.SET_NULL,
                                         null=True, blank=True)

    def __str__(self):
        return super().__str__() + \
               f' ({str(self.experiment)}, {str(self.subject)})'


class Content(GenericModel):
    """
    A.k.a. source. The source material where a Stimulus can be created based on.

    For example, a piece of Content can be a 5-second video clip chosen from
    Stranger Things.
    """

    name = models.CharField('name', max_length=1000)
    content_id = models.IntegerField('content id', null=True, blank=True)
    experiment: Experiment = models.ForeignKey(Experiment,
                                               on_delete=models.CASCADE,
                                               null=True, blank=True)

    def __str__(self):
        return super().__str__() + f' ({self.name}, {self.content_id})'
    
    def create_questplus(self, quest_config: dict, experiment: Experiment = None) -> 'QuestPlus':
        """
        Create a QuestPlus instance for this content.
        
        Args:
            quest_config: Configuration dictionary for QuestPlus
            experiment: Optional experiment to associate with
            
        Returns:
            QuestPlus: New QuestPlus instance
            
        Raises:
            ValueError: If QuestPlus already exists for this content
        """
        if hasattr(self, 'questplus'):
            raise ValueError(f"QuestPlus already exists for content {self.id}")
        
        # Import here to avoid circular imports - QuestPlus is defined in same module
        return QuestPlus.create_for_content(
            content=self,
            quest_config=quest_config,
            experiment=experiment or self.experiment
        )
    
    def get_questplus(self) -> Optional['QuestPlus']:
        """
        Get the QuestPlus instance associated with this content.
        
        Returns:
            QuestPlus instance or None if not found
        """
        try:
            return self.questplus
        except:
            return None


class Condition(GenericModel):
    """
    A.k.a. HRC, or Hypothetical Reference Circuits.

    In subjective testing lingo, a Condition determines how the Content is
    presented to a Subject. For example, a Condition can be "encoding a video
    at 1000 Kbps, and decoded and displayed it on a screen which is three times
    the screen height from the Subject's eyes".
    """

    name = models.CharField('name', max_length=1000)
    condition_id = models.IntegerField('condition id', null=True, blank=True)
    experiment: Experiment = models.ForeignKey(Experiment,
                                               on_delete=models.CASCADE,
                                               null=True, blank=True)

    def __str__(self):
        return super().__str__() + f' ({self.name})'


class Stimulus(GenericModel):
    """
    A.k.a PVS: Processed Video Sequence.

    A Stimulus is determined by its Content and the Condition it is presented
    in.

    Note that 'path' and 'type' are present in stimulus_config.stimuli but are
    not fields for Stimulus purposely. The reason is that we want path be
    easily changed due to moving the media files around but the db doesn't
    needs to be updated every time.
    """

    content = models.ForeignKey(Content, on_delete=models.SET_NULL, null=True,
                                blank=True)
    condition = models.ForeignKey(Condition, on_delete=models.SET_NULL,
                                  null=True, blank=True)
    stimulus_id = models.IntegerField('stimulus id', null=True, blank=True)
    distortion_level = models.FloatField('distortion level', null=True, blank=True,
                                       help_text='Distortion level for QuestPlus threshold matching')
    experiment: Experiment = models.ForeignKey(Experiment,
                                               on_delete=models.CASCADE,
                                               null=True, blank=True)

    def __str__(self):
        return (super().__str__() +
                f' ({self.content}, {self.condition}, {self.stimulus_id})')


class StimulusGroup(GenericModel):
    """
    One StimulusGroup contains multiple StimulusVoteGroups and is evaluated
    in each Round.
    """
    stimulusgroup_id = models.IntegerField('stimulus group id', null=True, blank=True)
    experiment: Experiment = models.ForeignKey(Experiment,
                                               on_delete=models.CASCADE,
                                               null=True, blank=True)

    @property
    def stimuli(self):
        stims = []
        for svg in StimulusVoteGroup.objects.filter(stimulusgroup=self):
            stims += svg.stimuli
        stims = sorted(set(stims), key=lambda x: x.id)
        return stims

    def __str__(self):
        return super().__str__() + f" ({self.stimulusgroup_id} : {','.join([str(stim.id) for stim in self.stimuli])})"


class StimulusVoteGroup(GenericModel):
    """
    A StimulusVoteGroup is a subset of a StimulusGroup. Each Vote is associated
    with a StimulusVoteGroup; each StimulusVoteGroup may have multiple votes.

    The relationship between StimulusVoteGroup and Stimulus is many-to-many:
    - Each Stimulus may belong to more than one StimulusVoteGroup.
    - Each StimulusVoteGroup may involve either one or two Stimuli, depending
    on if it is absolute (e.g. video A has “bad” quality) or relative scale
    (e.g. video A has “imperceptible” distortion compared to B). For the second
     case, the order matters, and this is preserved in the the VoteRegister
     (for example, if video A is order 1 and video B is order 2, then a vote
     of +1 represents that B is +1 better than A).
    """

    stimulus = models.ManyToManyField(Stimulus,
                                      through='VoteRegister',
                                      blank=True)
    stimulusgroup = models.ForeignKey(StimulusGroup,
                                      on_delete=models.CASCADE,
                                      null=True, blank=True)
    stimulusvotegroup_id = models.IntegerField('stimulus vote group id',
                                               null=True, blank=True)
    experiment: Experiment = models.ForeignKey(Experiment,
                                               on_delete=models.CASCADE,
                                               null=True, blank=True)

    @property
    def stimuli(self):
        stims = []
        for vr in VoteRegister.objects.filter(stimulusvotegroup=self):
            stims += [vr.stimulus]
        stims = sorted(set(stims), key=lambda x: x.id)
        return stims

    def __str__(self):
        return super().__str__() + f" ({','.join([str(stim.id) for stim in self.stimuli])})"

    @staticmethod
    def create_stimulusvotegroup_from_stimulus(stimulus: Stimulus,
                                               stimulusgroup: StimulusGroup = None,
                                               stimulusvotegroup_id: int = None,
                                               experiment: Experiment = None) -> StimulusVoteGroup:
        """
        create svg that corresponds to sg and a single stimulus
        """
        d = dict(stimulusgroup=stimulusgroup)
        if stimulusvotegroup_id is not None:
            d.update(dict(stimulusvotegroup_id=stimulusvotegroup_id))
        if experiment is not None:
            d.update(dict(experiment=experiment))

        svg = StimulusVoteGroup(**d)
        svg.save()
        VoteRegister.objects.create(
            stimulusvotegroup=svg,
            stimulus=stimulus,
        )
        return svg

    @staticmethod
    def find_stimulusvotegroups_from_stimulus(stim: Stimulus
                                              ) -> List[StimulusVoteGroup]:
        """
        find svgs that corresponds to single stimulus
        """

        svgs = StimulusVoteGroup.objects.filter(stimulus=stim)
        candidate_svgs = []
        for svg in svgs:
            vrs = VoteRegister.objects.filter(stimulus=stim,
                                              stimulus_order=1,
                                              stimulusvotegroup=svg)
            if vrs.count() > 0:
                assert vrs.count() == 1, \
                    f"expect a unique VoteRegister with svg and stim order 1," \
                    f" but got {vrs.count}: {vrs.all()}."
                candidate_svgs.append(svg)
        return candidate_svgs

    @staticmethod
    def create_stimulusvotegroup_from_stimuli_pair(stimulus1: Stimulus,
                                                   stimulus2: Stimulus,
                                                   stimulusgroup: StimulusGroup = None,
                                                   stimulusvotegroup_id: int = None,
                                                   experiment: Experiment = None) -> StimulusVoteGroup:
        """
        create svg that corresponds to sg and stimuli with order (stim1, stim2)
        """
        d = dict(stimulusgroup=stimulusgroup)
        if stimulusvotegroup_id is not None:
            d.update(dict(stimulusvotegroup_id=stimulusvotegroup_id))
        if experiment is not None:
            d.update(dict(experiment=experiment))
        svg = StimulusVoteGroup(**d)
        svg.save()
        VoteRegister.objects.create(
            stimulusvotegroup=svg,
            stimulus=stimulus1,
            stimulus_order=1,
        )
        VoteRegister.objects.create(
            stimulusvotegroup=svg,
            stimulus=stimulus2,
            stimulus_order=2,
        )
        return svg

    @staticmethod
    def find_stimulusvotegroups_from_stimuli_pair(stim1: Stimulus,
                                                  stim2: Stimulus
                                                  ) -> List[StimulusVoteGroup]:
        """
        find svgs that corresponds to stimuli with order (stim1, stim2)
        """

        # FIXME: the below logic seems convolved, although db_stress_test did
        #  not show much import/export time degradation as the db gets bigger
        svgs = (StimulusVoteGroup.objects.filter(stimulus=stim1)
                & StimulusVoteGroup.objects.filter(stimulus=stim2))
        candidate_svgs = []
        for svg in svgs:
            vrs = VoteRegister.objects.filter(stimulus=stim1,
                                              stimulus_order=1,
                                              stimulusvotegroup=svg)
            if vrs.count() > 0:
                assert vrs.count() == 1, \
                    f"expect a unique VoteRegister with svg and stim1 order 1 and " \
                    f"stim2 order 2, but got {vrs.count}: {vrs.all()}."
                candidate_svgs.append(svg)
        return candidate_svgs


class Round(GenericModel):
    """
    A short interval within a Session. It could be an interval where a Subject
    needs to evaluate the quality of a video Stimulus.
    """
    session: Session = models.ForeignKey(Session, on_delete=models.CASCADE)
    round_id: int = models.IntegerField('round id', null=True, blank=True)
    stimulusgroup: StimulusGroup = models.ForeignKey(StimulusGroup,
                                                     on_delete=models.SET_NULL,
                                                     null=True, blank=True)
    response_sec = models.FloatField('response sec', null=True, blank=True)

    def __str__(self):
        return super().__str__() + " ({}, {}, {})".format(
            self.session,
            self.round_id,
            self.stimulusgroup.stimulusgroup_id if
            self.stimulusgroup is not None else None
        )


class Vote(GenericModel, TypeVersionEnabled):
    """
    Abtract class for a Vote.

    It is associated with a Round of a Experiment Session, and can be
    associated with one (e.g. rate the quality of this video), two (e.g. rate
    the quality of the second video relative to the first video), or multiple
    (e.g. pick the best quality video from a list of videos) Stimuli.
    """
    round = models.ForeignKey(
        Round, on_delete=models.CASCADE,
        # the below are for convenience in tests since
        # don't want to create Round before creating Vote:
        null=True, blank=True,
    )
    stimulusvotegroup = models.ForeignKey(
        StimulusVoteGroup,
        on_delete=models.CASCADE,
        # the below are for convenience in tests since
        # don't want to create StimulusVoteGroup before
        # creating Vote:
        null=True, blank=True,
    )
    score = models.FloatField('score', null=False, blank=False)

    TYPE = 'VOTE'
    VERSION = '1.0'

    def __str__(self):
        return super().__str__() + f' ({self.score})'


class VoteRegister(GenericModel):
    """
    Register to associate a StimulusVoteGroup with a Stimulus.

    In NEST, the association between StimulusVoteGroup and Stimulus is
    many-to-many, and VoteRegister is the middle class to establish their
    association.

    For all the Stimuli associated with a StimulusVoteGroup, the order matters.
    For example, the first Stimulus is the reference and the second is the
    distorted, and the Vote associated with the StimulusVoteGroup is the
    quality of the distorted relative to the reference.
    """

    stimulusvotegroup = models.ForeignKey(StimulusVoteGroup, on_delete=models.CASCADE)
    stimulus = models.ForeignKey(Stimulus, on_delete=models.CASCADE)
    stimulus_order = models.PositiveIntegerField('stimulus order', default=1)


class DiscreteVote(Vote):
    """
    Abstract subclass of Vote, whose vote values come from a discrete set. For
    example, binary vote chosen from {0, 1}.
    """

    __metaclass__ = ABCMeta

    class Meta:
        abstract = True

    @property
    @abstractmethod
    def support(self):
        raise NotImplementedError

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'score' in kwargs:
            assert kwargs['score'] in self.support, \
                f"score {kwargs['score']} is not in the support {self.support}"

    @classmethod
    def assert_vote(cls, vote):
        assert vote in cls.support


class ContinuousVote(Vote):
    """
    Abstract subclass of Vote, whose vote values come from a continuous
    interval. For example, a real-value vote chosen in the interval of [0, 1].
    """

    __metaclass__ = ABCMeta

    class Meta:
        abstract = True

    @property
    @abstractmethod
    def range(self):
        raise NotImplementedError

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        assert len(self.range) == 2 and self.range[0] < self.range[
            1], f"illegal range {self.range}"
        if 'score' in kwargs:
            assert self.range[0] <= kwargs['score'] <= self.range[1], \
                f"score {kwargs['score']} is not in the range {self.range}"

    @classmethod
    def assert_vote(cls, vote):
        assert cls.range[0] <= vote <= cls.range[1]


class FivePointVote(DiscreteVote):
    """
    Discrete Vote from 1, 2, 3, 4, 5.
    """
    support = [1, 2, 3, 4, 5]
    TYPE = 'FIVE_POINT'


class TafcVote(DiscreteVote):
    """
    Two-alternative forced choice (2AFC) Vote from {0, 1}. aka "CcrTwoPointVote".
    """
    support = [0, 1]
    TYPE = '2AFC'


class CcrThreePointVote(DiscreteVote):
    """
    CCR (Comparison Category Rating) Three-point vote from {0, 1, 2}.
    """
    support = [0, 1, 2]
    TYPE = 'CCR_THREE_POINT'


class CcrFivePointVote(DiscreteVote):
    """
        CCR (Comparison Category Rating) Five-point vote from {0, 1, 2, 3, 4}.
        """
    support = [0, 1, 2, 3, 4]
    TYPE = 'CCR_FIVE_POINT'


class Zero2HundredVote(ContinuousVote):
    """
    Continuous Vote from the interval [0, 100].
    """
    range = [0, 100]
    TYPE = '0_TO_100'


class ElevenPointVote(DiscreteVote):
    """
    Discrete Vote from 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11
    """
    support = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    TYPE = 'ELEVEN_POINT'


class SevenPointVote(DiscreteVote):
    """
    Discrete Vote from 1, 2, 3, 4, 5, 6, 7
    """
    support = [1, 2, 3, 4, 5, 6, 7]
    TYPE = 'SEVEN_POINT'


class ThreePointVote(DiscreteVote):
    """
    Discrete Vote from 1, 2, 3
    """
    support = [1, 2, 3]
    TYPE = 'THREE_POINT'


class QuestPlus(GenericModel):
    """
    QuestPlus adaptive psychometric testing model.
    
    This model stores QuestPlus instances for adaptive testing experiments.
    Each QuestPlus instance is associated with a Content and stores its state
    as JSON data in the database.
    """
    
    content = models.OneToOneField(
        Content,
        on_delete=models.CASCADE,
        related_name='questplus'
    )
    
    experiment = models.ForeignKey(
        Experiment,
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )
    
    # Store QuestPlus configuration and state as JSON
    quest_config = models.JSONField(
        'QuestPlus configuration',
        help_text='Configuration parameters for QuestPlus instance'
    )
    
    quest_state = models.JSONField(
        'QuestPlus state',
        default=dict,
        blank=True,
        help_text='Current state of QuestPlus instance (serialized JSON)'
    )
    
    # # Path where JSON files are stored (for backward compatibility)
    # json_file_path = models.CharField(
    #     'JSON file path',
    #     max_length=500,
    #     blank=True,
    #     help_text='Optional file path for JSON storage'
    # )
    
    # Tracking fields
    total_trials = models.PositiveIntegerField(
        'Total trials',
        default=0,
        help_text='Number of trials completed'
    )
    
    is_active = models.BooleanField(
        'Is active',
        default=True,
        help_text='Whether this QuestPlus instance is currently active'
    )
    
    class Meta:
        verbose_name = 'QuestPlus Instance'
        verbose_name_plural = 'QuestPlus Instances'
    
    def __str__(self):
        return f"QuestPlus {self.id} ({self.content.name})"

    def _convert_ndarrays_to_lists(self, obj):
        import numpy as np
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        elif isinstance(obj, dict):
            return {k: self._convert_ndarrays_to_lists(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._convert_ndarrays_to_lists(i) for i in obj]
        else:
            return obj
    
    def _remove_instance_type(self, data):
        if "__instance_type__" in data:
            del data["__instance_type__"]
            print(f'Removed "__instance_type__"')
        return data
    
    def _add_instance_type(self, data, module_name="questplus.qp", class_name="QuestPlus"):
        data["__instance_type__"] = [module_name, class_name]
        return data

    def _remove_nans(self, data):
        import math

        if math.isnan(data['attributes']['entropy']):
            data['attributes']['entropy'] = None
        
        return data

    def _add_nans(self, data):

        if data['attributes']['entropy'] is None:
            data['attributes']['entropy'] = float('nan')
        
        return data
    
    def save_quest_to_json(self, quest_instance):
        """
        Save QuestPlus instance to database field.
        
        Args:
            quest_instance: The QuestPlus instance to serialize
                    """
        if not quest_instance:
            return None
            
        try:
            # # Generate file path if not set
            # if not self.json_file_path:
            #     filename = f"questplus_{self.content.id}_{self.id}.json"
            #     # Store in media directory alongside other experiment files
            #     media_dir = os.path.join(os.path.dirname(__file__), '..', 'media', 'questplus')
            #     os.makedirs(media_dir, exist_ok=True)
            #     self.json_file_path = os.path.join(media_dir, filename)
            
            # Serialize QuestPlus instance to JSON
            quest_json = quest_instance.to_json()
            
            # # Save to file
            # with open(self.json_file_path, 'w') as f:
            #     f.write(quest_json)
            
            # Also store in database field for direct access
            # Parse and re-serialize to ensure JSON compatibility
            quest_json = json.dumps(self._remove_instance_type(json.loads(quest_json)), allow_nan=True)
            parsed_data = json_tricks.loads(quest_json)
            parsed_data = self._convert_ndarrays_to_lists(parsed_data)
            parsed_data = self._remove_nans(parsed_data)

            filename = 'output2.json'
            with open(filename, 'w') as f:
                json.dump(parsed_data, f, indent=4, allow_nan=False)


            self.quest_state = parsed_data
            self.save()
            
            # return self.json_file_path
            
        except Exception as e:
            # Log error but don't raise - graceful degradation
            print(f"Warning: Failed to save QuestPlus to JSON file: {e}")
            return None
    
    def load_quest_from_json(self):
        """
        Load QuestPlus instance from database field.
        
        Returns:
            QuestPlus instance or None if loading failed
        """
        # try:
        #     # # Try loading from file first
        #     # if self.json_file_path and os.path.exists(self.json_file_path):
        #     #     with open(self.json_file_path, 'r') as f:
        #     #         quest_json = f.read()
        #     #     # This would require the actual questplus library
        #     #     # from questplus import QuestPlus as QP
        #     #     # return QP.from_json(quest_json)
        #     #     return quest_json
            
        #     # Fallback to database field
        from questplus import QuestPlus as QP
        if self.quest_state:
            quest_json = json.dumps(self._add_nans(self._add_instance_type(self.quest_state)), allow_nan=True)
            return QP.from_json(quest_json)
                
        else:
            print(f"Warning: Failed to load QuestPlus from JSON: {e}")
        
        return None
    
    def update_trial_count(self):
        """Update the total trial count."""
        self.total_trials += 1
        self.save()
    
    @classmethod
    def create_for_content(cls, content: Content, quest_config: dict, 
                          experiment: Experiment = None) -> 'QuestPlus':
        """
        Create a new QuestPlus instance for the given content.
        
        Args:
            content: Content instance to associate with
            quest_config: Configuration dictionary for QuestPlus
            experiment: Optional experiment to associate with
            
        Returns:
            QuestPlus: New instance
        """
        quest_plus = cls.objects.create(
            content=content,
            experiment=experiment,
            quest_config=quest_config
        )
        
        # Initialize the actual QuestPlus instance
        quest_plus._initialize_questplus_instance()
        
        return quest_plus
    
    def _initialize_questplus_instance(self):
        """
        Initialize the actual questplus.qp.QuestPlus instance using the configuration.
        """
        try:
            from questplus import QuestPlus as QP
            
            # Extract configuration parameters
            config = self.quest_config
            
            # Required parameters - convert numpy arrays to lists if needed
            stim_domain = config.get('stim_domain', {})
            param_domain = config.get('param_domain', {})
            outcome_domain = config.get('outcome_domain', {})
            func = config.get('func', 'weibull')
            
            # Optional parameters with defaults
            prior = config.get('prior', None)
            stim_scale = config.get('stim_scale', 'linear')
            stim_selection_method = config.get('stim_selection', 'min_entropy')
            stim_selection_options = config.get('stim_selection_options', None)
            param_estimation_method = config.get('param_estimation', 'mean')
            
            # Create QuestPlus instance
            qp_instance = QP(
                stim_domain=stim_domain,
                param_domain=param_domain,
                outcome_domain=outcome_domain,
                prior=prior,
                func=func,
                stim_scale=stim_scale,
                stim_selection_method=stim_selection_method,
                stim_selection_options=stim_selection_options,
                param_estimation_method=param_estimation_method
            )
            
            # Save the initialized instance state as JSON
            # import json
            # self.quest_state = json.loads(qp_instance.to_json())
            # self.save()
            self.save_quest_to_json(qp_instance)
            
            return qp_instance
            
        except ImportError as e:
            print(f"Warning: questplus library not available: {e}")
            # Initialize with empty state for graceful degradation
            self.quest_state = {}
            self.save()
            return None
        except Exception as e:
            print(f"Error initializing QuestPlus instance: {e}")
            self.quest_state = {}
            self.save()
            return None
    
    def get_next_stimulus(self):
        """
        Get the next optimal stimulus level using QuestPlus adaptive algorithm.
        
        Returns:
            Next stimulus level/threshold to test
        """
        try:
            
            # Load QuestPlus instance from saved state
            qp_instance = self._load_questplus_instance()
            
            if qp_instance is not None:
                # Get next stimulus using QuestPlus algorithm
                next_stim = qp_instance.next_stim
                return next_stim
            else:
                # Fallback: return middle value from config
                return self._get_fallback_stimulus()
                
        except ImportError:
            print("Warning: questplus library not available, using fallback")
            return self._get_fallback_stimulus()
        except Exception as e:
            print(f"Error getting next stimulus from QuestPlus: {e}")
            return self._get_fallback_stimulus()
    
    def _load_questplus_instance(self):
        """
        Load QuestPlus instance from saved JSON state.
        
        Returns:
            QuestPlus instance or None if loading fails
        """
        try:
            
            if self.quest_state:
                return self.load_quest_from_json()
            else:
                # No saved state, create new instance
                return self._initialize_questplus_instance()
                
        except Exception as e:
            print(f"Error loading QuestPlus instance: {e}")
            return None
    
    def _get_fallback_stimulus(self):
        """
        Get fallback stimulus when QuestPlus is not available.
        
        Returns:
            Fallback stimulus level
        """
        if self.quest_config and 'stim_domain' in self.quest_config:
            stim_levels = self.quest_config['stim_domain'].get('intensity', [1, 26, 51])
            if isinstance(stim_levels, list) and len(stim_levels) > 0:
                # Return middle value as a simple heuristic
                return stim_levels[len(stim_levels) // 2]
        
        # Default fallback
        return 26  # Middle of 1-51 range
    
    def update_with_response(self, stimulus_level, response) -> None:
        """
        Update QuestPlus instance with user response.
        
        Args:
            stimulus_level: The stimulus level that was presented
            response: User response ('correct'/'incorrect' for 2AFC)
        """
        try:            
            # Load current QuestPlus instance
            qp_instance = self._load_questplus_instance()
            
            if qp_instance is not None:
                # Update QuestPlus with new trial data
                qp_instance.update(stim=stimulus_level, outcome=response)
                
                # Save updated state back to database
                # Convert ndarrays to lists for storage
                # updated_data = json.loads(qp_instance.to_json())
                self.save_quest_to_json(qp_instance)   
                # self.quest_state = ndarray_to_list(updated_data)
                self.update_trial_count()
                # self.save()
                
                print(f"Updated QuestPlus with stimulus={stimulus_level}, response={response}")
            else:
                # Fallback: just update trial count and save basic info
                self._update_with_response_fallback(stimulus_level, response)
                
        except ImportError:
            print("Warning: questplus library not available, using fallback")
            self._update_with_response_fallback(stimulus_level, response)
        except Exception as e:
            print(f"Error updating QuestPlus with response: {e}")
            self._update_with_response_fallback(stimulus_level, response)
    
    def _update_with_response_fallback(self, stimulus_level, response):
        """
        Fallback method for updating response when QuestPlus library is not available.
        
        Args:
            stimulus_level: The stimulus level that was presented
            response: User response
        """
        self.update_trial_count()
        
        # Update quest_state with the new trial (simplified)
        if 'trials' not in self.quest_state:
            self.quest_state['trials'] = []
        
        self.quest_state['trials'].append({
            'stimulus_level': stimulus_level,
            'response': response,
            'trial_number': self.total_trials
        })
        
        self.save()
