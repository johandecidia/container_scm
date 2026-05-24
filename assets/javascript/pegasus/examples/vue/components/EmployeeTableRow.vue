<template>
  <tr>
    <td>{{ employee.name }}</td>
    <td>{{ employee.department }}</td>
    <td class="text-right">{{ formattedSalary }}</td>
    <td class="flex space-x-1 justify-end">
      <a class="btn btn-outline" v-on:click="editEmployee">
        <span class="w-6 h-6 inline-flex justify-center items-center"><i class="fa fa-edit" /></span>
        <span class="hidden md:inline-block">Edit</span>
      </a>
      <a class="btn btn-outline text-red-500 border-red-500 hover:bg-red-500 hover:border-red-700 hover:text-white" v-on:click="deleteEmployee">
        <span class="w-6 h-6 inline-flex justify-center items-center"><i class="fa fa-times" /></span>
        <span class="hidden md:inline-block">Delete</span>
      </a>
    </td>
  </tr>
</template>

<script>

export default {
  name: 'EmployeeTableRow',
  props: {
    employee: Object,
    client: Object,
  },
  computed: {
    formattedSalary: function () {
      const formatter = new Intl.NumberFormat('en-US', {
        style: 'currency',
        currency: 'USD',
      });
      return formatter.format(this.employee.salary);
    }
  },
  methods: {
    editEmployee: function () {
      this.$emit('edit-employee', this.employee);
    },
    deleteEmployee: function () {
      this.$emit('delete-employee', this.employee);
    },
  }
};
</script>

<style scoped>
</style>
