<template>
  <section class="app-card">
    <h1 class="text-xl mb-1">All Employees</h1>
    <div class="table-responsive">
      <table class="table table table-zebra w-full">
        <thead>
        <tr>
          <th>Name</th>
          <th>Department</th>
          <th class="text-right">Salary</th>
          <th></th>
        </tr>
        </thead>
        <tbody>
          <employee-table-row v-for="employee in employees"
                              v-bind:key="employee.id"
                              v-bind:employee="employee"
                              v-bind:client="client"
                              v-on:delete-employee="deleteEmployee"
                              v-on:edit-employee="editEmployee"
          />
        </tbody>
      </table>
    </div>
    <button class="btn btn-primary" v-on:click="addEmployee" >
      <span class="w-6 h-6 inline-flex justify-center items-center">
        <i class="fa fa-plus"></i>
      </span>
      <span>Add Employee</span>
    </button>
  </section>
</template>

<script>
import EmployeeTableRow from './EmployeeTableRow.vue'

export default {
  name: 'EmployeeList',
  components: {
    EmployeeTableRow
  },
  props: {
    employees: Array,
    client: Object,
  },
  methods: {
    addEmployee: function () {
      this.$emit('add-employee');
    },
    deleteEmployee: function (employee) {
      this.$emit('delete-employee', employee);
    },
    editEmployee: function (employee) {
      this.$emit('edit-employee', employee);
    },
  }
};
</script>
