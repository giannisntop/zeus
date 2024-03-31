# -*- coding: utf-8 -*-
import datetime
from time import strftime

from time import strptime, strftime
from django import forms #removed duplicate import
from django.forms import fields
from django.db import models
from django.template.loader import render_to_string
from django.forms.widgets import Select, MultiWidget, DateInput, TextInput

hour_selections = [("%02d:%02d" % (t, m), "%02d:%02d" % (t, m)) for t in range(24) for m in range(0, 60, 15)]

hour_selections.append(('23:59', '23:59'))

#divides date and time election data into two django widgets
class JqSplitDateTimeWidget(MultiWidget):

    def __init__(self, attrs=None, date_format=None, time_format=None):
        date_class = attrs['date_class']
        time_class = attrs['time_class']
        del attrs['date_class']
        del attrs['time_class']

        time_attrs = attrs.copy()
        time_attrs['class'] = time_class
        date_attrs = attrs.copy()
        date_attrs['class'] = date_class

        widgets = (DateInput(attrs=date_attrs, format=date_format),
                   Select(attrs=time_attrs, choices=hour_selections))

        super(JqSplitDateTimeWidget, self).__init__(widgets, attrs)

    def decompress(self, value):
        if value:
            if isinstance(value, tuple):
                return value[:2]

            timetuple = value.timetuple()
            d = strftime("%Y-%m-%d", timetuple)
            timeofday = strftime("%H:%M", timetuple)
            if not timeofday in list(dict(hour_selections).keys()):
                timeofday = strftime("%H:00",timetuple)
            return [d, timeofday]
        else:
            return (None, None, None, None)

    def format_output(self, rendered_widgets):
        """
        Given a list of rendered widgets (as strings), it inserts an HTML
        linebreak between them.

        Returns a Unicode string representing the HTML for the whole lot.
        """
        return """
        <div class="row"><div class="columns ten">%s</div>
        <div class="columns two" placeholder="">%s</div>
        </div>
        """ % (rendered_widgets[0], rendered_widgets[1])

#compression of date and time fields into one object
class JqSplitDateTimeField(fields.MultiValueField):
    widget = JqSplitDateTimeWidget

    def __init__(self, *args, **kwargs):
        """
        Have to pass a list of field types to the constructor, else we
        won't get any data to our compress method.
        """
        all_fields = (
            fields.CharField(max_length=10),
            fields.CharField(max_length=5),
            )

        super(JqSplitDateTimeField, self).__init__(all_fields, *args, **kwargs)

    def compress(self, data_list):
        """
        Takes the values from the MultiWidget and passes them as a
        list to this function. This function needs to compress the
        list into a single object to save.
        """
        if data_list:
            if not (data_list[0] and data_list[1]):
                raise forms.ValidationError("Field is missing data.")
            try:
                input_time = strptime("%s" %(data_list[1]), "%H:%M")
                datetime_string = "%s %s" % (data_list[0], strftime('%H:%M', input_time))
                return datetime.datetime(*strptime(datetime_string,
                                                "%Y-%m-%d %H:%M")[0:6])
            except ValueError:
                raise forms.ValidationError("Wrong date or time format")
        return None
