from datetime import datetime
import io as StringIO

from django.http import HttpResponseRedirect, HttpResponse
from django.core.urlresolvers import reverse
from django.conf import settings

from django.views.generic import View

from zeus.reports import ElectionsReportCSV, ElectionsReport
from zeus.utils import render_template, ELECTION_TABLE_HEADERS, \
    get_filters, ELECTION_SEARCH_FIELDS, ELECTION_BOOL_KEYS_MAP, \
    REPORT_TABLE_HEADERS, REPORT_SEARCH_FIELDS, REPORT_BOOL_KEYS_MAP

from zeus import auth

from helios.models import Election


class HomeView(View):
    """
    View class for handling home page related requests.
    """

    @auth.class_method
    @auth.election_admin_required
    def get(self, request, *args, **kwargs):
        """
        Handle HTTP GET requests for displaying the home page.
        """
        page = int(request.GET.get('page', 1))

        q_param = request.GET.get('q', '')

        default_elections_per_page = getattr(settings,
                                             'ELECTIONS_PER_PAGE', 20)
        elections_per_page = request.GET.get('limit',
                                             default_elections_per_page)
        try:
            elections_per_page = int(elections_per_page)
        except ValueError:
            elections_per_page = default_elections_per_page
        order_by = request.GET.get('order', 'created_at')
        order_type = request.GET.get('order_type', 'desc')
        if order_by not in ELECTION_TABLE_HEADERS:
            order_by = 'name'

        elections = Election.objects.administered_by(request.admin)
        nr_unfiltered_elections = elections.count()
        if nr_unfiltered_elections == 0:
            return HttpResponseRedirect(reverse('election_create'))

        elections = elections.filter(get_filters(q_param,
                                                 ELECTION_TABLE_HEADERS,
                                                 ELECTION_SEARCH_FIELDS,
                                                 ELECTION_BOOL_KEYS_MAP))
        elections = elections.order_by(order_by)
        if order_type == 'desc':
            elections = elections.reverse()

        context = {
            'is_superadmin': request.admin.superadmin_p,
            'elections_administered': elections,
            'election_table_headers': iter(ELECTION_TABLE_HEADERS.items()),
            'q': q_param,
            'page': page,
            'elections_per_page': elections_per_page,
        }
        return render_template(request, "index", context)

    @auth.class_method
    @auth.manager_or_superadmin_required
    def post(self, request, *args, **kwargs):
        """
        Handle HTTP POST requests to update the official status of elections.
        """
        official = request.POST.getlist('official', '')
        uuid = request.POST.getlist('uuid', None)

        for status, election_uuid in zip(official, uuid):
            try:
                election = Election.objects.get(uuid=election_uuid)
                if status == '':
                    status = None
                else:
                    status = int(status)

                election.official = status
                election.save()
            except Election.DoesNotExist:
                pass
            except ValueError:
                pass

        return HttpResponseRedirect(reverse('admin_home'))


def find_elections(request):
    """
    Find elections based on various filters provided in the request parameters.
    """
    order_by = request.GET.get('order', 'completed_at')
    order_type = request.GET.get('order_type', 'desc')
    start_date = request.GET.get('start_date', None)
    end_date = request.GET.get('end_date', None)
    q = request.GET.get('q', '')

    # basic filters
    basic_filter = {
        'trial': False,
        'completed_at__isnull': False,
    }

    """
    _all = request.GET.get('full', 0)
    if not _all:
        filter['include_in_reports'] = True
    """

    # filter by date
    if start_date:
        basic_filter['voting_starts_at__gte'] = datetime.strptime(start_date,
                                                            "%d %b %Y")

    if end_date:
        basic_filter['voting_starts_at__lte'] = datetime.strptime(end_date,
                                                            "%d %b %Y")

    # filter by query
    q_filters = get_filters(
        q,
        REPORT_TABLE_HEADERS,
        REPORT_SEARCH_FIELDS,
        REPORT_BOOL_KEYS_MAP
    )

    if order_by not in ELECTION_TABLE_HEADERS:
        order_by = 'completed_at'

    elections = Election.objects.filter(**basic_filter).order_by(order_by)
    elections = elections.filter(q_filters)

    if order_type == 'desc':
        elections = elections.reverse()

    return elections


@auth.manager_or_superadmin_required
def elections_report_csv(request):
    """
    Create and download a CSV elections report
    """
    elections = find_elections(request)

    report = ElectionsReportCSV(elections)
    csv_path = getattr(settings, 'ZEUS_ELECTIONS_REPORT_INCLUDE', None)
    if csv_path:
        report.parse_csv(csv_path)
    report.parse_object()
    # ext is not needed
    date = datetime.datetime.now()
    str_date = date.strftime("%Y-%m-%d")
    filename = 'elections_report_' + str_date
    fd = StringIO.StringIO()
    report.make_output(fd)
    fd.seek(0)

    response = HttpResponse(fd, content_type='application/csv')
    response['Content-Disposition'] = f'attachment; filename={filename}.csv'
    return response


@auth.manager_or_superadmin_required
def elections_report(request):
    """
    Creates an elections report
    """
    q_param = request.GET.get('q', '')

    polls_count = voters_count = 0
    percentage_voted = voters_voted_count = 0

    elections = find_elections(request)

    report = ElectionsReport(elections)
    report.parse_object()

    elections_count = len(report.objectData)
    for election in report.objectData:
        polls_count += election['nr_polls']
        voters_count += election['nr_voters']
        voters_voted_count += election['nr_voters_voted']

    if voters_count != 0:
        percentage_voted = (voters_voted_count / float(voters_count)) * 100

    params = ''
    for key, value in list(request.GET.items()):
        params = params + key + '=' + value + '&'
    params = params[:-1]

    context = {
        'elections_count': elections_count,
        'polls_count': polls_count,
        'voters_count': voters_count,
        'voters_voted_count': voters_voted_count,
        'percentage_voted': percentage_voted,
        'elections': report.objectData,
        'elections_per_page': 10,
        'report_table_headers': list(REPORT_TABLE_HEADERS.items()),
        'params': params,
        'q': q_param
    }

    return render_template(request, "admin", context)
