# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe

from frappe.utils import getdate, validate_email_add, today
from frappe.model.naming import make_autoname
from frappe import throw, _
import frappe.permissions
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from erpnext.utilities.transaction_base import delete_events


class EmployeeUserDisabledError(frappe.ValidationError):
	pass


class Employee(Document):
	def onload(self):
		self.set_onload('links', self.meta.get_links_setup())

	def autoname(self):
		naming_method = frappe.db.get_value("HR Settings", None, "emp_created_by")
		if not naming_method:
			throw(_("Please setup Employee Naming System in Human Resource > HR Settings"))
		else:
			if naming_method == 'Naming Series':
				self.name = make_autoname(self.naming_series + '.####')
			elif naming_method == 'Employee Number':
				self.name = self.employee_number

		self.employee = self.name

	def validate(self):
		from erpnext.controllers.status_updater import validate_status
		validate_status(self.status, ["Active", "Left"])

		self.employee = self.name
		self.validate_date()
		self.validate_email()
		self.validate_status()
		self.validate_employee_leave_approver()
		self.validate_reports_to()

		if self.user_id:
			self.validate_for_enabled_user_id()
			self.validate_duplicate_user_id()
		else:
			existing_user_id = frappe.db.get_value("Employee", self.name, "user_id")
			if existing_user_id:
				frappe.permissions.remove_user_permission(
					"Employee", self.name, existing_user_id)

	def on_update(self):
		if self.user_id:
			self.update_user()
			self.update_user_permissions()

	def update_user_permissions(self):
		frappe.permissions.add_user_permission("Employee", self.name, self.user_id)
		frappe.permissions.set_user_permission_if_allowed("Company", self.company, self.user_id)

	def update_user(self):
		# add employee role if missing
		user = frappe.get_doc("User", self.user_id)
		user.flags.ignore_permissions = True

		if "Employee" not in user.get("user_roles"):
			user.add_roles("Employee")

		# copy details like Fullname, DOB and Image to User
		if self.employee_name and not (user.first_name and user.last_name):
			employee_name = self.employee_name.split(" ")
			if len(employee_name) >= 3:
				user.last_name = " ".join(employee_name[2:])
				user.middle_name = employee_name[1]
			elif len(employee_name) == 2:
				user.last_name = employee_name[1]

			user.first_name = employee_name[0]

		if self.date_of_birth:
			user.birth_date = self.date_of_birth

		if self.gender:
			user.gender = self.gender

		if self.image:
			if not user.user_image:
				user.user_image = self.image
				try:
					frappe.get_doc({
						"doctype": "File",
						"file_name": self.image,
						"attached_to_doctype": "User",
						"attached_to_name": self.user_id
					}).insert()
				except frappe.DuplicateEntryError:
					# already exists
					pass

		user.save()

	def validate_date(self):
		if self.date_of_birth and getdate(self.date_of_birth) > getdate(today()):
			throw(_("Date of Birth cannot be greater than today."))

		if self.date_of_birth and self.date_of_joining and getdate(self.date_of_birth) >= getdate(self.date_of_joining):
			throw(_("Date of Joining must be greater than Date of Birth"))

		elif self.date_of_retirement and self.date_of_joining and (getdate(self.date_of_retirement) <= getdate(self.date_of_joining)):
			throw(_("Date Of Retirement must be greater than Date of Joining"))

		elif self.relieving_date and self.date_of_joining and (getdate(self.relieving_date) <= getdate(self.date_of_joining)):
			throw(_("Relieving Date must be greater than Date of Joining"))

		elif self.contract_end_date and self.date_of_joining and (getdate(self.contract_end_date) <= getdate(self.date_of_joining)):
			throw(_("Contract End Date must be greater than Date of Joining"))

	def validate_email(self):
		if self.company_email:
			validate_email_add(self.company_email, True)
		if self.personal_email:
			validate_email_add(self.personal_email, True)

	def validate_status(self):
		if self.status == 'Left' and not self.relieving_date:
			throw(_("Please enter relieving date."))

	def validate_for_enabled_user_id(self):
		if not self.status == 'Active':
			return
		enabled = frappe.db.get_value("User", self.user_id, "enabled")
		if enabled is None:
			frappe.throw(_("User {0} does not exist").format(self.user_id))
		if enabled == 0:
			frappe.throw(_("User {0} is disabled").format(self.user_id), EmployeeUserDisabledError)

	def validate_duplicate_user_id(self):
		employee = frappe.db.sql_list("""select name from `tabEmployee` where
			user_id=%s and status='Active' and name!=%s""", (self.user_id, self.name))
		if employee:
			throw(_("User {0} is already assigned to Employee {1}").format(
				self.user_id, employee[0]), frappe.DuplicateEntryError)

	def validate_employee_leave_approver(self):
		for l in self.get("leave_approvers")[:]:
			if "Leave Approver" not in frappe.get_roles(l.leave_approver):
				frappe.get_doc("User", l.leave_approver).add_roles("Leave Approver")

	def validate_reports_to(self):
		if self.reports_to == self.name:
			throw(_("Employee cannot report to himself."))

	def on_trash(self):
		delete_events(self.doctype, self.name)

	def get_timeline_data(self):
		'''returns timeline data based on attendance'''
		return

@frappe.whitelist()
def get_dashboard_data(name):
	'''load dashboard related data'''
	frappe.has_permission(doc=frappe.get_doc('Employee', name), throw=True)

	from frappe.desk.notifications import get_open_count
	return {
		'count': get_open_count('Employee', name),
		'timeline_data': get_timeline_data(name),
	}

def get_timeline_data(name):
	'''Return timeline for attendance'''
	return dict(frappe.db.sql('''select unix_timestamp(att_date), count(*)
		from `tabAttendance` where employee=%s
			and att_date > date_sub(curdate(), interval 1 year)
			and status in ('Present', 'Half Day')
			group by att_date''', name))

@frappe.whitelist()
def get_retirement_date(date_of_birth=None):
	import datetime
	ret = {}
	if date_of_birth:
		try:
			dt = getdate(date_of_birth) + datetime.timedelta(21915)
			ret = {'date_of_retirement': dt.strftime('%Y-%m-%d')}
		except ValueError:
			# invalid date
			ret = {}

	return ret


@frappe.whitelist()
def make_salary_structure(source_name, target=None):
	target = get_mapped_doc("Employee", source_name, {
		"Employee": {
			"doctype": "Salary Structure",
			"field_map": {
				"name": "employee",
			}
		}
	})
	target.make_earn_ded_table()
	return target

def validate_employee_role(doc, method):
	# called via User hook
	if "Employee" in [d.role for d in doc.get("user_roles")]:
		if not frappe.db.get_value("Employee", {"user_id": doc.name}):
			frappe.msgprint(_("Please set User ID field in an Employee record to set Employee Role"))
			doc.get("user_roles").remove(doc.get("user_roles", {"role": "Employee"})[0])

def update_user_permissions(doc, method):
	# called via User hook
	if "Employee" in [d.role for d in doc.get("user_roles")]:
		employee = frappe.get_doc("Employee", {"user_id": doc.name})
		employee.update_user_permissions()

def send_birthday_reminders():
	"""Send Employee birthday reminders if no 'Stop Birthday Reminders' is not set."""
	if int(frappe.db.get_single_value("HR Settings", "stop_birthday_reminders") or 0):
		return

	from frappe.utils.user import get_enabled_system_users
	users = None

	birthdays = get_employees_who_are_born_today()

	if birthdays:
		if not users:
			users = [u.email_id or u.name for u in get_enabled_system_users()]

		for e in birthdays:
			frappe.sendmail(recipients=filter(lambda u: u not in (e.company_email, e.personal_email, e.user_id), users),
				subject=_("Birthday Reminder for {0}").format(e.employee_name),
				message=_("""Today is {0}'s birthday!""").format(e.employee_name),
				reply_to=e.company_email or e.personal_email or e.user_id,
				bulk=True)

def get_employees_who_are_born_today():
	"""Get Employee properties whose birthday is today."""
	return frappe.db.sql("""select name, personal_email, company_email, user_id, employee_name
		from tabEmployee where day(date_of_birth) = day(%(date)s)
		and month(date_of_birth) = month(%(date)s)
		and status = 'Active'""", {"date": today()}, as_dict=True)

def get_holiday_list_for_employee(employee, raise_exception=True):
	holiday_list, company = frappe.db.get_value("Employee", employee, ["holiday_list", "company"])

	if not holiday_list:
		holiday_list = frappe.db.get_value("Company", company, "default_holiday_list")

	if not holiday_list and raise_exception:
		frappe.throw(_('Please set a default Holiday List for Employee {0} or Company {0}').format(employee, company))

	return holiday_list

