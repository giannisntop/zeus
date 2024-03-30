# -*- coding: utf-8 -*-
"""
Forms for Zeus
"""
import re
import copy
import json
import types

from collections import defaultdict
from datetime import datetime, timedelta

from django import forms
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext_lazy as _
from django.conf import settings
from django.utils.safestring import mark_safe
from django.contrib.auth.hashers import check_password, make_password
from django.forms.models import BaseModelFormSet
from django.forms.widgets import Select, MultiWidget, TextInput, HiddenInput
from django.forms.formsets import BaseFormSet

from helios.models import Election, Poll, Voter
from heliosauth.models import User

from zeus.utils import extract_trustees
from zeus.utils import election_trustees_to_text, resolve_terms_options
from zeus.widgets import JqSplitDateTimeField, JqSplitDateTimeWidget
from zeus import help_texts as help
from zeus.utils import ordered_dict_prepend

from django.core.validators import validate_email
from zeus.election_modules import ELECTION_MODULES_CHOICES
from zeus import taxisnet

from zeus.utils import parse_markdown_unsafe


LOG_CHANGED_FIELDS = [
    "name",
    "voting_starts_at",
    "voting_ends_at",
    "voting_extended_until",
    "description",
    "help_email",
    "help_phone",
]

INVALID_CHAR_MSG = _("%s is not a valid character.")


def election_form_formfield_cb(f, **kwargs):
    if f.name in [
        "voting_starts_at",
        "voting_ends_at",
        "voting_extended_until",
        "forum_starts_at",
        "forum_ends_at",
        "forum_extended_until",
    ]:
        widget = JqSplitDateTimeWidget(
            attrs={"date_class": "datepicker", "time_class": "timepicker"}
        )
        return JqSplitDateTimeField(
            label=f.verbose_name,
            initial=f.default,
            widget=widget,
            required=not f.blank,
            help_text=f.help_text,
        )
    return f.formfield()


def setup_editable_fields(form, **value_overrides):
    """
    Django versions prior to 1.9 do not support disabled form fields. This
    injects fields and associated widgets in order for values of non editable
    fields to be displayed as non editable and also to not get validated and
    updated even if changed in form data. Disabling fields is based on
    FIELD_REQUIRED_FEATURES mapping which maps field names to model features.
    A field is editable only when all required model features resolve to True.
    """
    for name, features in form.FIELD_REQUIRED_FEATURES.items():
        editable = all([form.instance.check_feature(f) for f in features])

        field = form.fields.get(name)
        if not field:
            continue
        if not editable:
            disable_field(field, name, form, value_overrides)


def disable_field(field, name, form, value_overrides):
    widget = field.widget
    widget.attrs["readonly"] = True
    widget.attrs["disabled"] = True
    field.disabled = True  # Django 1.9 only

    # creates a value_from_datadict which returns instance value instead
    # of form data one
    def _mk_readonly(widget, name, form, override=None):
        def readonly_value_from_datadict(self, *args, **kwargs):
            if override:
                value = override(form)
            else:
                value = getattr(form.instance, name)
            if hasattr(widget, "decompress"):
                value = widget.decompress(value)
            return value

        return types.MethodType(readonly_value_from_datadict, widget)

    widget.__dict__["value_from_datadict"] = _mk_readonly(
        widget, name, form, value_overrides.get(name, None)
    )

    def _mk_dummy_clean(form, name):
        def _dummy_clean(self, *args, **kwargs):
            return self.cleaned_data.get(name)

        return types.MethodType(_dummy_clean, form)

    form.__dict__["clean_%s" % name] = _mk_dummy_clean(form, name)

    if isinstance(widget, forms.CheckboxInput):
        widget.attrs["disabled"] = True


class ElectionForm(forms.ModelForm):

    formfield_callback = election_form_formfield_cb

    trustees = forms.CharField(
        label=_("Trustees"),
        required=False,
        widget=forms.Textarea,
        help_text=help.trustees,
    )
    remote_mixes = forms.BooleanField(
        label=_("Multiple mixnets"), required=False,help_text=help.remote_mixes
    )
    legal_representative = forms.CharField(
        label=_("Legal representative"), required=True, help_text=None
    )
    terms_consent = forms.BooleanField(
        label=_("Terms consent"), required=True, help_text=None
    )
    FIELD_REQUIRED_FEATURES = {
        "trustees": ["edit_trustees"],
        "name": ["edit_name"],
        "description": ["edit_description"],
        "election_module": ["edit_type"],
        "voting_starts_at": ["edit_voting_starts_at"],
        "voting_ends_at": ["edit_voting_ends_at"],
        "voting_extended_until": ["edit_voting_extended_until"],
        "remote_mixes": ["edit_remote_mixes"],
        "trial": ["edit_trial"],
        "departments": ["edit_departments"],
        "cast_consent_text": ["edit_cast_consent_text"],
    }

    class Meta:
        model = Election
        fields = (
            "trial",
            "election_module",
            "name",
            "description",
            "departments",
            "voting_starts_at",
            "voting_ends_at",
            "voting_extended_until",
            "trustees",
            "help_email",
            "help_phone",
            "communication_language",
            "sms_api_enabled",
            "cast_consent_text",
        )

    def __init__(self, owner, institution, *args, **kwargs):
        self.institution = institution
        self.owner = owner
        self.terms_text = None

        if kwargs.get("lang"):
            lang = kwargs.pop("lang")
        else:
            lang = None
        super(ElectionForm, self).__init__(*args, **kwargs)
        choices = getattr(settings, "LANGUAGES", [])
        choices = [(x[0], _(x[1])) for x in choices]
        help_text = _("Set the language that will be used for email messages")
        self.fields["communication_language"] = forms.ChoiceField(
            label=_("Communication language"),
            choices=choices,
            initial=lang,
            help_text=help_text,
        )
        # self.fields['linked_polls'].widget = forms.HiddenInput()
        if owner.sms_data:
            help_text = (
                _("Notify voters using SMS (%d "
                  +"deliveries available for your account)")
                % owner.sms_data.left
            )
            self.fields["sms_api_enabled"] = forms.BooleanField(
                label=_("Mobile SMS notifications enabled"),
                initial=True,
                required=False,
                help_text=help_text,
            )
        else:
            del self.fields["sms_api_enabled"]

        self.creating = True
        self._initial_data = {}
        election_owner = owner
        if self.instance and self.instance.pk:
            self._initial_data = {}
            for field in LOG_CHANGED_FIELDS:
                self._initial_data[field] = self.initial[field]
            self.creating = False
            election_owner = self.instance.admins.filter()[0]

        # user-specific terms text
        terms_options = resolve_terms_options(election_owner)
        self.terms_text = parse_markdown_unsafe(_(terms_options.get("terms_text")))
        self.fields["terms_consent"].help_text = self.terms_text
        # terms consent is required and only available during election creation
        # otherwise, enforce value to True and disable the checkbox input
        if not self.creating:
            self.fields["terms_consent"].widget.attrs["disabled"] = True
            self.fields["terms_consent"].widget.attrs["checked"] = True
            self.fields["terms_consent"].required = False

        if not terms_options.get("require_legal_representative"):
            del self.fields["legal_representative"]
        else:
            if not self.creating:
                self.fields["legal_representative"].widget.attrs["disabled"] = True
                self.fields["legal_representative"].required = False
                self.fields["legal_representative"].initial = (
                    self.instance.legal_representative
                )

        eligible_types = owner.eligible_election_types
        if not self.creating and self.instance:
            eligible_types.add(self.instance.election_module)
        eligible_types_choices = [
            x for x in ELECTION_MODULES_CHOICES if x[0] in eligible_types
        ]

        self.fields["election_module"].choices = eligible_types_choices
        deps_labels = {
            "unicouncilsgr": str(_("Departments")),
            "stv": str(_("Constituencies")),
        }
        deps_help_texts = {
            "unicouncilsgr": str(
                _(
                    "University Schools. e.g."
                    "<br/><br/> School of Engineering <br />"
                    "School of Medicine<br />School of"
                    "Informatics<br />"
                )
            ),
            "stv": str(
                _(
                    "List of constituencies. e.g."
                    "<br/><br/>District A<br />"
                    "District B<br />District C"
                    "<br />"
                )
            ),
        }
        self.fields["departments"].widget.attrs["data-labels"] = json.dumps(deps_labels)
        self.fields["departments"].widget.attrs["data-help"] = json.dumps(
            deps_help_texts
        )
        self.fields["departments"].required = False

        _module = self.data.get("election_module", None)
        _module = _module or (self.instance and self.instance.election_module)
        if _module in ["unicouncilsgr"]:
            self.fields["departments"].required = True

        if self.instance and self.instance.pk:
            self.fields.get("trustees").initial = election_trustees_to_text(
                self.instance
            )
            self.fields.get("remote_mixes").initial = bool(self.instance.mix_key)

        def _clean_trustees(form):
            return election_trustees_to_text(form.instance)

        def _clean_remote_mixes(form):
            return bool(form.instance.mix_key)

        value_overrides = {
            "trustees": _clean_trustees,
            "remote_mixes": _clean_remote_mixes,
        }
        setup_editable_fields(self, **value_overrides)

        if not self.instance.frozen_at:
            self.fields.pop("voting_extended_until")

    def clean(self):
        data = super(ElectionForm, self).clean()
        self.clean_voting_dates(
            data.get("voting_starts_at"),
            data.get("voting_ends_at"),
            data.get("voting_extended_until"),
        )

        return data

    def clean_departments(self):
        deps = self.cleaned_data.get("departments")
        deps_arr = deps.split("\n")
        cleaned_deps = []
        for item in deps_arr:
            item = item.strip()
            item = item.lstrip()
            if item:
                cleaned_deps.append(item)
        cleaned_deps = "\n".join(cleaned_deps)
        return cleaned_deps

    def clean_voting_dates(self, starts, ends, extension):
        # WARN: skip live validation here. warn user during freeze instead.
        # final_ends = extension or ends
        # if self.instance and self.instance.polls.count():
        # for poll in self.instance.polls.filter():
        # if not poll.forum_enabled:
        # continue
        # if starts and poll.forum_starts_at < starts:
        # raise forms.ValidationError(_("Invalid voting start date. Check poll forum access dates."))
        # if final_ends and poll.forum_ends_at > final_ends:
        # raise forms.ValidationError(_("Invalid voting end date. Check poll forum access dates."))
        if starts and ends:
            if ends < datetime.now() and self.instance.feature_edit_voting_ends_at:
                raise forms.ValidationError(_("Invalid voting end date"))
            if starts >= ends:
                raise forms.ValidationError(_("Invalid voting dates"))
        if extension and extension <= ends:
            raise forms.ValidationError(_("Invalid voting extension date"))

    def clean_trustees(self):
        trustees = self.cleaned_data.get("trustees")
        try:
            for tname, temail in extract_trustees(trustees):
                validate_email(temail)
        except:
            raise forms.ValidationError(_("Invalid trustees format"))
        return trustees

    def log_changed_fields(self, instance):
        for field in LOG_CHANGED_FIELDS:
            if field in self.changed_data:
                inital = self._initial_data[field]
                newvalue = self.cleaned_data[field]
                instance.logger.info(
                    "Field '%s' changed from %r to %r", field, inital, newvalue
                )

    def save(self, *args, **kwargs):
        remote_mixes = self.cleaned_data.get("remote_mixes")
        if remote_mixes:
            self.instance.generate_mix_key()
        else:
            self.instance.mix_key = None
        saved = super(ElectionForm, self).save(*args, **kwargs)
        trustees = extract_trustees(self.cleaned_data.get("trustees"))
        saved.institution = self.institution
        if saved.sms_api_enabled:
            saved.sms_data = self.owner.sms_data

        if self.creating:
            saved.legal_representative = self.cleaned_data.get(
                "legal_representative", None
            )
        saved.save()
        if saved.feature_edit_trustees:
            saved.update_trustees(trustees)
        else:
            saved.logger.info("Election updated %r", self.changed_data)
            self.log_changed_fields(saved)
        return saved


class AnswerWidget(forms.TextInput):

    def render(self, *args, **kwargs):
        html = super(AnswerWidget, self).render(*args, **kwargs)
        html = (
            """
        <div class="row">
        <div class="columns eleven">
        %s
        </div>
        <div class="columns one">
        <a href="#" style="font-weight: bold; color:red"
        class="remove_answer">X</a>
        </div>
        </div>
        """
            % html
        )
        return mark_safe(html)


DEFAULT_ANSWERS_COUNT = 2
MAX_QUESTIONS_LIMIT = getattr(settings, "MAX_QUESTIONS_LIMIT", 1)


class QuestionBaseForm(forms.Form):
    choice_type = forms.ChoiceField(choices=(("choice", _("Choice")),))
    question = forms.CharField(
        label=_("Question"),
        max_length=5000,
        required=True,
        widget=forms.Textarea(attrs={"rows": 4, "class": "textarea"}),
    )

    def __init__(self, *args, **kwargs):
        super(QuestionBaseForm, self).__init__(*args, **kwargs)
        if len(self.fields["choice_type"].choices) == 1:
            self.fields["choice_type"].widget = forms.HiddenInput()
            self.fields["choice_type"].initial = "choice"

        answers = len(
            [k for k in self.data if k.startswith("%s-answer_" % self.prefix)]
        )
        if not answers:
            answers = len(
                [
                    k
                    for k in list(self.initial.keys())
                    if k.startswith("answer_") and not "indexes" in k
                ]
            )

        if answers == 0:
            answers = DEFAULT_ANSWERS_COUNT

        for ans in range(answers):
            field_key = "answer_%d" % ans
            self.fields[field_key] = forms.CharField(
                max_length=300, required=True, widget=AnswerWidget
            )
            self.fields[field_key].widget.attrs = {"class": "answer_input"}

        self._answers = answers

    def clean_question(self):
        q = self.cleaned_data.get("question", "")
        if "%" in q:
            raise forms.ValidationError(INVALID_CHAR_MSG % "%")
        return q.replace(": ", ":\t")


class QuestionForm(QuestionBaseForm):
    min_answers = forms.ChoiceField(label=_("Min answers"))
    max_answers = forms.ChoiceField(label=_("Max answers"))

    min_limit = None
    max_limit = None

    def __init__(self, *args, **kwargs):
        super(QuestionForm, self).__init__(*args, **kwargs)
        answers = self._answers
        max_choices = [(x, x) for x in range(1, self.max_limit or answers + 1)]
        
        self.fields["max_answers"].choices = max_choices
        self.fields["max_answers"].initial = min([x[1] for x in max_choices])
        self.fields["min_answers"].choices = max_choices
        self.fields["min_answers"].initial = 0

    def clean(self):
        max_answers = int(self.cleaned_data.get("max_answers"))
        min_answers = int(self.cleaned_data.get("min_answers"))
        if min_answers > max_answers:
            raise forms.ValidationError(
                _("Max answers should be greater " "or equal to min answers")
            )
        answer_list = []
        for key in self.cleaned_data:
            if key.startswith("answer_"):
                if "%" in self.cleaned_data[key]:
                    raise forms.ValidationError(INVALID_CHAR_MSG % "%")
                answer_list.append(self.cleaned_data[key])
        if len(answer_list) > len(set(answer_list)):
            raise forms.ValidationError(_("No duplicate choices allowed"))
        return self.cleaned_data


class PartyForm(QuestionForm):
    question = forms.CharField(
        label=_("Party name"),
        max_length=255,
        required=True,
        widget=forms.Textarea(attrs={"rows": 4, "class": "textarea"}),
    )


SCORES_DEFAULT_LEN = 2
SCORES_CHOICES = [(x, x) for x in range(1, 10)]


class ScoresForm(QuestionBaseForm):
    scores = forms.MultipleChoiceField(
        required=True,
        widget=forms.CheckboxSelectMultiple,
        choices=SCORES_CHOICES,
        label=_("Scores"),
    )

    scores.initial = (1, 2)

    min_answers = forms.ChoiceField(label=_("Min answers"), required=True)
    max_answers = forms.ChoiceField(label=_("Max answers"), required=True)

    def __init__(self, *args, **kwargs):
        super(ScoresForm, self).__init__(*args, **kwargs)
        if type(self.data) != dict:
            myDict = dict(self.data.iterlists())
        else:
            myDict = self.data

        if "form-0-scores" in myDict:
            self._scores_len = len(myDict["form-0-scores"])
        elif "scores" in self.initial:
            self._scores_len = len(self.initial["scores"])
        else:
            self._scores_len = SCORES_DEFAULT_LEN
        max_choices = [(x, x) for x in range(1, self._scores_len + 1)]
        self.fields["max_answers"].choices = max_choices
        self.fields["max_answers"].initial = self._scores_len
        self.fields["min_answers"].choices = max_choices

    def clean(self):
        super(ScoresForm, self).clean()
        max_answers = int(self.cleaned_data.get("max_answers", 0))
        min_answers = int(self.cleaned_data.get("min_answers", 0))
        if (min_answers and max_answers) and min_answers > max_answers:
            raise forms.ValidationError(
                _("Max answers should be greater " "or equal to min answers")
            )
        answer_list = []
        for key in self.cleaned_data:
            if key.startswith("answer_"):
                if "%" in self.cleaned_data[key]:
                    raise forms.ValidationError(INVALID_CHAR_MSG % "%")
                answer_list.append(self.cleaned_data[key])
        if len(answer_list) > len(set(answer_list)):
            raise forms.ValidationError(_("No duplicate choices allowed"))
        if "scores" in self.cleaned_data:
            if len(answer_list) < max_answers:
                m = _("Number of answers must be equal or bigger than max answers")
                raise forms.ValidationError(m)
        return self.cleaned_data


class RequiredFormset(BaseFormSet):

    def __init__(self, *args, **kwargs):
        super(RequiredFormset, self).__init__(*args, **kwargs)
        try:
            self.forms[0].empty_permitted = False
        except IndexError:
            pass


class CandidateWidget(MultiWidget):

    def __init__(self, *args, **kwargs):
        departments = kwargs.pop("departments", [])
        widgets = (TextInput(), Select(choices=departments))
        super(CandidateWidget, self).__init__(widgets, *args, **kwargs)

    def decompress(self, value):
        if not value:
            return [None, None]

        return json.loads(value)

    def format_output(self, rendered_widgets):
        """
        Given a list of rendered widgets (as strings), it inserts an HTML
        linebreak between them.

        Returns a Unicode string representing the HTML for the whole lot.
        """
        return """
        <div class="row answer_input"><div class="columns nine">%s</div>
        <div class="columns two" placeholder="">%s</div>
        <div class="columns one">
        <a href="#" style="font-weight: bold; color:red"
        class="remove_answer">X</a>
        </div>
        </div>
        """ % (
            rendered_widgets[0],
            rendered_widgets[1],
        )

    def value_from_datadict(self, data, files, name):
        datalist = [
            widget.value_from_datadict(data, files, name + "_%s" % i)
            for i, widget in enumerate(self.widgets)
        ]
        return json.dumps(datalist)


class StvForm(QuestionBaseForm):

    answer_widget_values_len = 2

    def __init__(self, *args, **kwargs):
        deps = kwargs["initial"].get("departments_data", "").split("\n")

        DEPARTMENT_CHOICES = []
        for dep in deps:
            DEPARTMENT_CHOICES.append((dep.strip(), dep.strip()))

        super(StvForm, self).__init__(*args, **kwargs)

        self.fields.pop("question")
        answers = (
            len([k for k in self.data if k.startswith("%s-answer_" % self.prefix)])
            / self.answer_widget_values_len
        )

        if not answers:
            answers = len([k for k in self.initial if k.startswith("answer_")])
        if answers == 0:
            answers = DEFAULT_ANSWERS_COUNT

        self.fields.clear()
        for ans in range(answers):
            field_key = "answer_%d" % ans
            field_key1 = "department_%d" % ans
            _widget = self._make_candidate_widget(DEPARTMENT_CHOICES)
            self.fields[field_key] = forms.CharField(
                max_length=600, required=True, widget=_widget, label=("Candidate")
            )

        elig_help_text = _("https://en.wikipedia.org/wiki/Droop_quota")
        label_text = _("Droop quota")
        ordered_dict_prepend(
            self.fields,
            "droop_quota",
            forms.BooleanField(
                required=False, label=label_text, initial=True, help_text=elig_help_text
            ),
        )
        self.fields["droop_quota"].widget.attrs["readonly"] = True
        self.fields["droop_quota"].widget.attrs["disabled"] = True

        widget = forms.TextInput(attrs={"hidden": "True"})
        dep_lim_help_text = _("maximum number of elected from the same constituency")
        dep_lim_label = _("Constituency limit")
        ordered_dict_prepend(
            self.fields,
            "department_limit",
            forms.CharField(
                help_text=dep_lim_help_text,
                label=dep_lim_label,
                widget=widget,
                required=False,
            ),
        )

        widget = forms.CheckboxInput()
        limit_help_text = _("enable limiting the elections from the same constituency")
        limit_label = _("Limit elected per constituency")
        ordered_dict_prepend(
            self.fields,
            "has_department_limit",
            forms.BooleanField(
                widget=widget,
                help_text=limit_help_text,
                label=limit_label,
                required=False,
            ),
        )

        elig_help_text = _("set the eligibles count of the election")
        label_text = _("Eligibles count")
        ordered_dict_prepend(
            self.fields,
            "eligibles",
            forms.CharField(label=label_text, help_text=elig_help_text),
        )

    min_answers = None
    max_answers = None

    def _make_candidate_widget(self, departments):
        return CandidateWidget(departments=departments)

    def _clean_answer(self, answer):
        from django.forms.util import ErrorList

        answer_lst = json.loads(answer)
        if "%" in answer_lst[0]:
            raise forms.ValidationError(INVALID_CHAR_MSG % "%")
        if not answer_lst[0]:
            message = _("This field is required.")
            self._errors["answer_0"] = ErrorList([message])
            return None, json.dumps([])
        answer_lst[0] = answer_lst[0].strip()
        return answer_lst[0], json.dumps(answer_lst)

    def clean(self):
        answers = (
            len([k for k in self.data if k.startswith("%s-answer_" % self.prefix)])
            / self.answer_widget_values_len
        )

        # list used for checking duplicate candidates
        candidates_list = []

        for ans in range(answers):
            field_key = "answer_%d" % ans
            answer = self.cleaned_data[field_key]
            key, cleaned = self._clean_answer(answer)
            candidates_list.append(key)
            self.cleaned_data[field_key] = cleaned

        if len(candidates_list) > len(set(candidates_list)):
            raise forms.ValidationError(_("No duplicate choices allowed"))

        self.cleaned_data["droop_quota"] = True
        return self.cleaned_data

    def clean_eligibles(self):
        message = _("Value must be a positve integer")
        eligibles = self.cleaned_data.get("eligibles")
        try:
            eligibles = int(eligibles)
            if eligibles > 0:
                return eligibles
            else:
                raise forms.ValidationError(message)
        except ValueError as TypeError:
            raise forms.ValidationError(message)

    def clean_department_limit(self):
        message = _("Value must be a positve integer")
        dep_limit = self.cleaned_data.get("department_limit")
        if self.cleaned_data.get("has_department_limit"):
            if not dep_limit:
                raise forms.ValidationError(message)
        else:
            return 0
        try:
            dep_limit = int(dep_limit)
            if dep_limit > 0:
                return dep_limit
            else:
                raise forms.ValidationError(message)
        except ValueError:
            raise forms.ValidationError(message)


class UniCouncilsGrForm(StvForm):

    def __init__(self, *args, **kwargs):
        super(UniCouncilsGrForm, self).__init__(*args, **kwargs)
        has_limit_help_text = _(
            "enable limiting the elections from the same department"
        )
        has_limit_label = _("Limit elected per department")
        self.fields["has_department_limit"].label = has_limit_label
        self.fields["has_department_limit"].help_text = has_limit_label
        dep_lim_help_text = _("maximum number of elected from the same department")
        dep_lim_label = _("Department limit")
        self.fields["department_limit"].label = dep_lim_label
        self.fields["department_limit"].help_text = dep_lim_help_text
        del self.fields["droop_quota"]

    def clean(self):
        super(UniCouncilsGrForm, self).clean()
        self.cleaned_data["droop_quota"] = False
        return self.cleaned_data


class PreferencesForm(StvForm):

    answer_widget_values_len = 1

    def __init__(self, *args, **kwargs):
        super(PreferencesForm, self).__init__(*args, **kwargs)
        del self.fields["department_limit"]
        del self.fields["has_department_limit"]
        del self.fields["eligibles"]
        del self.fields["droop_quota"]

    def _make_candidate_widget(self, departments):
        return AnswerWidget(attrs={"class": "answer_input"})

    def _clean_answer(self, answer):
        if "%" in answer:
            raise forms.ValidationError(INVALID_CHAR_MSG % "%")
        return answer, answer


class LoginForm(forms.Form):
    username = forms.CharField(label=_("Username"), max_length=50)
    password = forms.CharField(
        label=_("Password"), widget=forms.PasswordInput(), max_length=100
    )

    def clean(self):
        self._user_cache = None
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        try:
            user = User.objects.get(user_id=username)
        except User.DoesNotExist:
            raise forms.ValidationError(_("Invalid username or password"))

        if user.is_disabled:
            raise forms.ValidationError(_("Your account is disabled"))

        if check_password(password, user.info["password"]):
            self._user_cache = user
            return self.cleaned_data
        else:
            raise forms.ValidationError(_("Invalid username or password"))


class PollForm(forms.ModelForm):

    FIELD_REQUIRED_FEATURES = {
        "name": ["edit_name"],
        "forum_enabled": ["edit_forum"],
        "forum_ends_at": ["edit_forum"],
        "forum_description": ["edit_forum"],
        "forum_starts_at": ["edit_forum"],
        "forum_extended_until": ["edit_forum_extension"],
        "linked_ref": ["edit_linked_ref"],
        "taxisnet_auth": ["edit_taxisnet"],
    }

    formfield_callback = election_form_formfield_cb

    forum_enabled = forms.BooleanField(
        label=_("Poll forum enabled"), required=False, help_text=help.forum_enabled
    )
    linked_ref = forms.ChoiceField(
        required=False, initial="", label=_("Link voters list to another poll")
    )

    def __init__(self, *args, **kwargs):
        self.election = kwargs.pop("election", None)
        self.admin = kwargs.pop("admin", None)

        super(PollForm, self).__init__(*args, **kwargs)
        if "linked_ref" in self.initial and self.initial["linked_ref"] is None:
            self.initial["linked_ref"] = ""
        CHOICES = (
            ("public", "public"),
            ("confidential", "confidential"),
        )

        TYPES = (("google", "google"), ("facebook", "facfebook"), ("other", "other"))

        ordered_dict_prepend(
            self.fields,
            "jwt_file",
            forms.FileField(label="JWT public keyfile", required=False),
        )

        if self.instance.is_linked_root:
            del self.fields["linked_ref"]
        else:
            linked_choices = [["", ""]]
            for p in self.election.polls.filter().exclude(pk=self.instance.pk):
                if not p.is_linked_leaf:
                    linked_choices.append((p.uuid, p.name))
            self.fields["linked_ref"].choices = linked_choices

        self.fields["jwt_file"].widget.attrs["accept"] = ".pem"
        self.fields["jwt_public_key"] = forms.CharField(
            required=False, widget=forms.Textarea
        )
        self.fields["oauth2_type"] = forms.ChoiceField(required=False, choices=TYPES)
        self.fields["oauth2_client_type"] = forms.ChoiceField(
            required=False, choices=CHOICES
        )
        self.fields["google_code_url"] = forms.CharField(
            widget=HiddenInput,
            initial="https://accounts.google.com/o/oauth2/auth",
            required=False,
        )
        self.fields["google_exchange_url"] = forms.CharField(
            widget=HiddenInput,
            initial="https://accounts.google.com/o/oauth2/token",
            required=False,
        )
        self.fields["google_confirmation_url"] = forms.CharField(
            widget=HiddenInput,
            initial="https://www.googleapis.com/oauth2/v1/userinfo",
            required=False,
        )
        self.fields["facebook_code_url"] = forms.CharField(
            widget=HiddenInput,
            initial="https://www.facebook.com/dialog/oauth",
            required=False,
        )
        self.fields["facebook_exchange_url"] = forms.CharField(
            widget=HiddenInput,
            initial="https://graph.facebook.com/oauth/access_token",
            required=False,
        )
        self.fields["facebook_confirmation_url"] = forms.CharField(
            widget=HiddenInput,
            initial="https://graph.facebook.com/v2.2/me",
            required=False,
        )

        self.fields["forum_starts_at"].help_text = None
        self.fields["forum_ends_at"].help_text = (
            _("Voting starts at %s") % self.election.voting_ends_at
        )

        shib_data = None
        if self.initial is not None:
            shib_data = self.initial.get("shibboleth_constraints", None)
            if isinstance(shib_data, str):
                shib_data = json.loads(shib)
            if shib_data is not None and isinstance(shib_data, dict):
                self.initial["shibboleth_constraints"] = json.dumps(shib_data)
            if not self.instance or not self.instance.pk:
                self.initial["forum_ends_at"] = self.election.voting_starts_at
                self.initial["forum_starts_at"] = self.initial[
                    "forum_ends_at"
                ] - timedelta(days=2)

        if self.election.feature_frozen:
            self.fields["name"].widget.attrs["readonly"] = True

        auth_title = _("2-factor authentication")
        auth_help = _("2-factor authentication help text")
        self.fieldsets = {"auth": [auth_title, auth_help, []]}
        self.fieldset_fields = []

        profiles = getattr(settings, "ZEUS_SHIBBOLETH_PROFILES", {})
        self.shib_profiles = profiles
        extra_auth_fields = {}
        auth_fields = ["jwt", "google", "facebook", "shibboleth", "oauth2"]
        auth_checks = ["jwt_auth", "oauth2_thirdparty", "shibboleth_auth"]

        if taxisnet.is_enabled(self.admin) or self.instance.taxisnet_auth:
            auth_fields.append("taxisnet")
            auth_checks.append("taxisnet_auth")
        else:
            del self.fields["taxisnet_auth"]

        if profiles:
            for key, data in list(profiles.items()):
                field_key = "shibprofile{}".format(key)
                fields_key = "{}_auth".format(field_key)
                if shib_data and shib_data.get("profile", None) == key:
                    del self.initial["shibboleth_constraints"]
                    del self.initial["shibboleth_auth"]
                    self.initial[fields_key] = True
                widget = forms.CheckboxInput()
                field = forms.BooleanField(
                    widget=widget,
                    help_text=data.get("help_text", " "),
                    label=data.get("label", ""),
                    required=False,
                )
                self.fields[fields_key] = field
                self.fieldset_fields.append(field)
                auth_checks.append(fields_key)
                auth_fields.append(field_key)

        for name, field in list(self.fields.items()):
            parts = name.split("_")
            if not parts[0] in auth_fields:
                continue
            is_check = name in auth_checks
            key = parts[0]

            if key in auth_fields:
                self.fieldsets["auth"][2].append(name)
                self.fieldset_fields.append(field)
                setattr(field, "field_attrs", "")
                attrs = "data-auth={}".format(key)
                if is_check:
                    attrs += " data-auth-toggle=true"
                    field.widget.attrs["field_class"] = "fieldset-auth"
                    field.help_text = field.help_text or "&nbsp;&nbsp;"
                else:
                    attrs += " data-auth-option={}".format(key)
                    field.widget.attrs["field_class"] = "auth-option-field {}".format(
                        key
                    )
                field.field_attrs = attrs

        keyOrder = self.fieldsets["auth"][2]
        fieldsKeys = list(self.fields.keys())
        for field in auth_checks:
            prev_index = keyOrder.index(field)
            item = keyOrder.pop(prev_index)
            keyOrder.insert(0, item)
            if field == "jwt_auth":
                self.fields[field].widget.attrs["field_class"] = "clearfix last"

        if self.admin and not self.admin.can_enable_forum:
            if not self.instance.forum_enabled:
                del self.fields["forum_enabled"]
        self.instance.election = self.election
        setup_editable_fields(self)
        disable_auth = False
        for f in auth_checks:
            field = self.fields[f]
            if getattr(field, "disabled", False):
                disable_auth = True
        if disable_auth:
            for f in auth_checks:
                field = self.fields[f]
                if not getattr(field, "disabled", False):
                    disable_field(field, f, self, {})

    class Meta:
        model = Poll
        fields = (
            "name",
            "jwt_auth",
            "jwt_issuer",
            "jwt_public_key",
            "oauth2_thirdparty",
            "oauth2_type",
            "oauth2_client_type",
            "oauth2_client_id",
            "oauth2_client_secret",
            "oauth2_code_url",
            "oauth2_exchange_url",
            "oauth2_confirmation_url",
            "shibboleth_auth",
            "shibboleth_constraints",
            "forum_enabled",
            "forum_description",
            "forum_starts_at",
            "forum_ends_at",
            "forum_extended_until",
            "linked_ref",
            "taxisnet_auth",
        )

    def iter_fieldset(self, name):
        for field in self.fieldsets[name][2]:
            yield self[field]

    def clean_linked_ref(self):
        ref = self.cleaned_data.get("linked_ref", None)
        if not ref:
            ref = None
        if ref:
            if not ref in self.election.polls.filter().values_list("uuid", flat=True):
                raise forms.ValidationError(_("Invalid poll"))
            else:
                p = self.election.polls.get(uuid=ref)
                if p.is_linked_leaf:
                    raise forms.ValidationError(_("Invalid poll"))

        return ref

    def clean_forum_starts_at(self):
        # forum start date should be set on a date after current date.
        enabled = self.cleaned_data.get("forum_enabled")
        starts_at = self.cleaned_data.get("forum_starts_at")
        if enabled and not starts_at:
            raise forms.ValidationError(_("This field is required."))
        voting_starts = self.election.voting_starts_at
        if not self.election.trial and enabled and starts_at >= voting_starts:
            raise forms.ValidationError(_("Forum should start before voting."))
        return starts_at

    def clean_forum_ends_at(self):
        # forum end date should be set if forum is enabled and should be set to
        # a date after current date and after forum start date
        enabled = self.cleaned_data.get("forum_enabled")
        starts_at = self.cleaned_data.get("forum_starts_at")
        ends_at = self.cleaned_data.get("forum_ends_at")
        voting_starts = self.election.voting_starts_at
        if enabled and not ends_at:
            raise forms.ValidationError(_("This field is required."))
        if all([enabled, ends_at, starts_at]) and (ends_at <= starts_at):
            raise forms.ValidationError(_("Invalid forum access end date"))
        if enabled and not self.election.trial and (ends_at > voting_starts):
            raise forms.ValidationError(_("Forum should end before voting."))

        return ends_at

    def clean_forum_description(self):
        desc = self.cleaned_data.get("forum_description") or ""
        enabled = self.cleaned_data.get("forum_enabled")

        desc = desc.strip()
        if enabled and not desc:
            raise forms.ValidationError(_("This field is required."))
        return desc

    def clean_forum_extended_until(self):
        date = self.cleaned_data.get("forum_extended_until")
        enabled = self.cleaned_data.get("forum_enabled")
        forum_ends_at = self.instance.forum_ends_at

        if enabled and date and (date <= forum_ends_at):
            raise forms.ValidationError(_("Invalid forum extension date."))
        return date

    def clean_shibboleth_constraints(self):
        value = self.cleaned_data.get("shibboleth_constraints", None)
        if value == "None":
            return None
        try:
            value and json.loads(value)
        except Exception as e:
            raise forms.ValidationError(_("Invalid shibboleth constraints."))

        return value

    def clean(self):
        super(PollForm, self).clean()

        data = self.cleaned_data
        election_polls = self.election.polls.all()

        enabled = self.cleaned_data.get("forum_enabled")
        linked_ref = self.cleaned_data.get("linked_ref")
        if enabled and linked_ref:
            msg = [_("Forum cannot be enabled for linked polls")]
            self._errors["forum_enabled"] = msg
            self._errors["linked_ref"] = msg

        for poll in election_polls:
            if data.get("name") == poll.name and (
                (not self.instance.pk)
                or (self.instance.pk and self.instance.name != data.get("name"))
            ):
                message = _("Duplicate poll names are not allowed")
                raise forms.ValidationError(message)
        if self.election.feature_frozen and (
            self.cleaned_data["name"] != self.instance.name
        ):
            raise forms.ValidationError(
                _(
                    "Poll name cannot be changed\
                                               after freeze"
                )
            )

        oauth2_field_names = [
            "type",
            "client_type",
            "client_id",
            "client_secret",
            "code_url",
            "exchange_url",
            "confirmation_url",
        ]
        oauth2_field_names = ["oauth2_" + x for x in oauth2_field_names]
        jwt_field_names = ["jwt_issuer", "jwt_public_key"]
        url_validate = URLValidator()
        if data["oauth2_thirdparty"]:
            for field_name in oauth2_field_names:
                if not data[field_name]:
                    self._errors[field_name] = (_("This field is required."),)
            url_types = ["code", "exchange", "confirmation"]
            for url_type in url_types:
                try:
                    url_validate(data["oauth2_{}_url".format(url_type)])
                except ValidationError:
                    self._errors["oauth2_{}_url".format(url_type)] = (
                        _("This URL is invalid"),
                    )
        else:
            for field_name in oauth2_field_names:
                data[field_name] = ""

        shibboleth_field_names = []
        if data["shibboleth_auth"]:
            for field_name in shibboleth_field_names:
                if not data[field_name]:
                    self._errors[field_name] = (_("This field is required."),)

        for _key, item in list(self.shib_profiles.items()):
            key = "shibprofile{}_auth".format(_key)
            if data[key]:
                data["shibboleth_auth"] = True
                data["shibboleth_constraints"] = item.get("data")
                data["shibboleth_constraints"]["profile"] = _key

        if data["jwt_auth"]:
            for field_name in jwt_field_names:
                if not data[field_name]:
                    self._errors[field_name] = (_("This field is required."),)
        else:
            for field_name in jwt_field_names:
                data[field_name] = ""

        return data

    def save(self, *args, **kwargs):
        was_linked = self.initial.get("linked_ref", None)
        commit = kwargs.pop("commit", True)
        instance = super(PollForm, self).save(commit=False, *args, **kwargs)
        instance.election = self.election
        is_new = instance.pk is None
        if commit:
            instance.save()
        if (was_linked != instance.linked_ref) and instance.linked_ref:
            if instance.linked_to_poll.feature_can_sync_voters:
                instance.linked_to_poll.sync_linked_voters()
        return instance


class PollFormSet(BaseModelFormSet):

    def __init__(self, *args, **kwargs):
        self.election = kwargs.pop("election", None)
        self.admin = kwargs.pop("admin", None)
        super(PollFormSet, self).__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        kwargs["election"] = kwargs.get("election", self.election)
        kwargs["admin"] = kwargs.get("admin", self.admin)
        return super(PollFormSet, self)._construct_form(i, **kwargs)

    def clean(self):
        forms_data = self.cleaned_data
        form_poll_names = []
        for form_data in forms_data:
            form_poll_names.append(form_data["name"])
            poll_name = form_data["name"]
            e = Election.objects.get(id=self.election.id)
            election_polls = e.polls.all()
            for poll in election_polls:
                if poll_name == poll.name:
                    message = _("Duplicate poll names are not allowed")
                    raise forms.ValidationError(message)
        if len(form_poll_names) > len(set(form_poll_names)):
            message = _("Duplicate poll names are not allowed")
            raise forms.ValidationError(message)

    def save(self, election, *args, **kwargs):
        commit = kwargs.pop("commit", True)
        instances = super(PollFormSet, self).save(commit=False, *args, **kwargs)
        if commit:
            for instance in instances:
                instance.election = election
                instance.save()

        return instances


SEND_TO_CHOICES = [
    ("all", _("all selected voters")),
    ("voted", _("selected voters who have cast a ballot")),
    ("not-voted", _("selected voters who have not yet cast a ballot")),
]

CONTACT_CHOICES = [
    ("email", _("Email only")),
    ("sms", _("SMS only")),
    ("email:sms", _("Email and SMS")),
]


class EmailVotersForm(forms.Form):
    email_subject = forms.CharField(
        label=_("Email subject"), max_length=80, required=False
    )
    email_body = forms.CharField(
        label=_("In place of BODY"),
        max_length=30000,
        widget=forms.Textarea,
        required=False,
    )
    sms_body = forms.CharField(
        label=_("In place of SMS_BODY"),
        max_length=30000,
        widget=forms.Textarea,
        required=False,
    )
    contact_method = forms.ChoiceField(
        label=_("Contact method"), initial="email:sms", choices=CONTACT_CHOICES
    )
    notify_once = forms.BooleanField(
        initial=True, label=_("Do not send sms if voter email is set"), required=False
    )
    send_to = forms.ChoiceField(
        label=_("Send To"), initial="all", choices=SEND_TO_CHOICES
    )

    def __init__(self, election, template, *args, **kwargs):
        super(EmailVotersForm, self).__init__(*args, **kwargs)
        self.election = election
        self.template = template

        if not election.sms_enabled:
            self.fields["sms_body"].widget = forms.HiddenInput()
            self.fields["contact_method"].widget = forms.HiddenInput()
            self.fields["contact_method"].choices = [("email", _("Email"))]
            self.fields["contact_method"].initial = "email"
            self.fields["notify_once"].widget = forms.HiddenInput()
            self.fields["notify_once"].initial = False
        else:
            choices = copy.copy(CONTACT_CHOICES)
            choices[1] = list(choices[1])
            choices[1][1] = "%s (%s)" % (
                str(choices[1][1]),
                _("%d deliveries available") % election.sms_data.left,
            )
            self.fields["contact_method"].choices = choices

    def clean(self):
        super(EmailVotersForm, self).clean()
        data = self.cleaned_data
        if "sms" in data.get("contact_method", []) and self.template == "info":
            if data.get("sms_body").strip() == "":
                raise ValidationError(_("Please provide SMS body"))
        return data


class ChangePasswordForm(forms.Form):
    password = forms.CharField(label=_("Current password"), widget=forms.PasswordInput)
    new_password = forms.CharField(label=_("New password"), widget=forms.PasswordInput)
    new_password_confirm = forms.CharField(
        label=_("New password confirm"), widget=forms.PasswordInput
    )

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super(ChangePasswordForm, self).__init__(*args, **kwargs)

    def save(self):
        user = self.user
        pwd = make_password(self.cleaned_data["new_password"].strip())
        user.info["password"] = pwd
        user.save()

    def clean(self):
        cl = super(ChangePasswordForm, self).clean()
        pwd = self.cleaned_data["password"].strip()
        if not check_password(pwd, self.user.info["password"]):
            raise forms.ValidationError(_("Invalid password"))
        if not self.cleaned_data.get("new_password") == self.cleaned_data.get(
            "new_password_confirm"
        ):
            raise forms.ValidationError(_("Passwords don't match"))
        return cl


class VoterLoginForm(forms.Form):

    login_id = forms.CharField(label=_("Login password"), required=True)
    validation = re.compile("[0-9]{1,10}-(?:[0-9]{4}-){3,}[0-9]{4}")
    validation_digits = re.compile("[0-9]{17,}")

    def __init__(self, *args, **kwargs):
        self._voter = None
        super(VoterLoginForm, self).__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super(VoterLoginForm, self).clean()

        login_id = self.cleaned_data.get("login_id", "").strip()

        invalid_login_id_error = _("Invalid login code")
        if not login_id:
            raise forms.ValidationError(invalid_login_id_error)

        matches = list(filter(bool, self.validation.findall(login_id)))
        if len(matches):
            login_id = matches[0]
        else:
            matches = list(filter(bool, self.validation_digits.findall(login_id)))
            if len(matches):
                login_id = matches[0]

        try:
            poll_id, secret = Voter.extract_login_code(login_id)
        except ValueError:
            raise forms.ValidationError(invalid_login_id_error)

        poll = None
        try:
            poll = Poll.objects.get(pk=poll_id)
        except Poll.DoesNotExist:
            raise forms.ValidationError(invalid_login_id_error)
        except ValueError:
            raise forms.ValidationError(invalid_login_id_error)

        try:
            self._voter = poll.voters.get(voter_password=secret)
        except Voter.DoesNotExist:
            raise forms.ValidationError(_("Invalid email or password"))

        return cleaned_data


class STVBallotForm(forms.Form):

    def __init__(self, *args, **kwargs):
        candidates = self.candidates
        super(STVBallotForm, self).__init__(*args, **kwargs)
        choices = [("", "")]
        for i, c in enumerate(candidates):
            choices.append((str(i), c))
        for i, c in enumerate(candidates):
            self.fields["choice_%d" % (i + 1)] = forms.ChoiceField(
                choices=choices,
                initial="",
                required=False,
                label=_("Ballot choice %s") % str(i + 1),
            )

    def get_choices(self, serial):
        vote = {"votes": [], "ballotSerialNumber": serial}
        for i, c in enumerate(self.candidates):
            val = self.cleaned_data.get("choice_%d" % (i + 1), "")
            if not val:
                break
            vote["votes"].append({"rank": (i + 1), "candidateTmpId": val})
        return vote

    def clean(self):
        data = self.cleaned_data
        empty = False
        choices = []
        for i, c in enumerate(self.candidates):
            val = self.cleaned_data.get("choice_%d" % (i + 1), "")
            if val == "":
                empty = True
            if val and empty:
                raise ValidationError(_("Invalid ballot"))
            if val:
                choices.append(val)

        if len(choices) != len(set(choices)):
            raise ValidationError(_("Invalid ballot"))
        return data


candidates_help_text = _(
    """Candidates list. e.g., <br/><br/>
FirstName, LastName, FatherName, SchoolA<br />
FirstName, LastName, FatherName, SchoolB<br />
"""
)

limit_choices = [(x, str(x)) for x in range(2)]
eligibles_choices = [(x, str(x)) for x in range(1, 20)]


class STVElectionForm(forms.Form):
    name = forms.CharField(label=_("Election name"), required=True)
    voting_starts = forms.CharField(
        label=_("Voting start date"),
        required=True,
        help_text=_("e.g. 25/01/2015 07:00"),
    )
    voting_ends = forms.CharField(
        label=_("Voting end date"), required=True, help_text=_("e.g. 25/01/2015 19:00")
    )
    institution = forms.CharField(label=_("Institution name"))
    candidates = forms.CharField(
        label=_("Candidates"), widget=forms.Textarea, help_text=candidates_help_text
    )
    eligibles_count = forms.ChoiceField(
        label=_("Eligibles count"), choices=eligibles_choices
    )
    elected_limit = forms.IntegerField(
        label=_("Maximum elected per department"), required=False
    )
    ballots_count = forms.CharField(label=_("Ballots count"))

    def __init__(self, *args, **kwargs):
        kwargs.pop("disabled", False)
        super(STVElectionForm, self).__init__(*args, **kwargs)

    def clean_voting_starts(self):
        d = self.cleaned_data.get("voting_starts") or ""
        d = d.strip()
        try:
            datetime.strptime(d, "%d/%m/%Y %H:%M")
        except:
            raise ValidationError(_("Invalid date format"))
        return d

    def clean_voting_ends(self):
        d = self.cleaned_data.get("voting_ends") or ""
        d = d.strip()
        try:
            datetime.strptime(d, "%d/%m/%Y %H:%M")
        except:
            raise ValidationError(_("Invalid date format"))
        return d

    def clean_candidates(self):
        candidates = self.cleaned_data.get("candidates").strip()
        candidates = [x.strip() for x in candidates.split("\n")]
        for c in candidates:
            if len(c.split(",")) != 4:
                raise ValidationError(_("Candidate %s is invalid") % c)

        return candidates

    def get_candidates(self):
        if not hasattr(self, "cleaned_data"):
            return []

        cs = self.cleaned_data.get("candidates")[:]
        for i, c in enumerate(cs):
            cs[i] = [x.strip().replace(" ", "-") for x in c.split(",")]
            cs[i] = "{} {} {}:{}".format(*cs[i])
        return cs

    def get_data(self):
        data = self.cleaned_data
        ret = {}
        ret["elName"] = data.get("name")
        ret["electedLimit"] = data.get("elected_limit") or 0
        ret["votingStarts"] = data.get("voting_starts")
        ret["votingEnds"] = data.get("voting_ends")
        ret["institution"] = data.get("institution")
        ret["numOfEligibles"] = int(data.get("eligibles_count"))
        cands = self.get_candidates()
        schools = defaultdict(lambda: [])
        for i, c in enumerate(cands):
            name, school = c.split(":")
            name, surname, fathername = name.split(" ")
            entry = {
                "lastName": surname,
                "fatherName": fathername,
                "candidateTmpId": i,
                "firstName": name,
            }
            schools[school].append(entry)

        _schools = []
        for school, cands in schools.items():
            _schools.append({"candidates": cands, "Name": school})

        ret["schools"] = _schools
        ret["ballots"] = []
        return ret
