<template>
  <section class="app-card">
    <form>
      <h2 class="text-xl mb-1">Employee Details</h2>
      <div class="mb-3">
        <label class="block font-bold">Name</label>
        <input class="input w-full" type="text" placeholder="Michael Scott" v-model="name">
        <p class="text-sm text-base-content/70">Your employee's name.</p>
      </div>
      <div class="mb-3">
        <label class="block font-bold">Department</label>
        <div class="pg-select">
          <select v-model="department">
            <option v-for="option in departmentOptions" v-bind:value="option.id">
              {{ option.name }}
            </option>
          </select>
        </div>
        <p class="text-sm text-base-content/70">What department your employee belongs to.</p></div>
      <div class="mb-3">
        <label class="block font-bold">Salary</label>
        <input class="input w-full" type="number" min="0" placeholder="50000" v-model="salary">
        <p class="text-sm text-base-content/70">Your employee's annual salary.</p></div>
      <div class="flex space-x-1">
        <button class="btn btn-primary" v-on:click.prevent="saveEdit">
          <span class="w-6 h-6 inline-flex justify-center items-center"><i :class="getSaveIconClass"></i></span>
          <span>{{ getSaveIconText }}</span>
        </button>
        <button class="btn btn-ghost mx-2" v-on:click.prevent="$emit('cancel-edit')">
          <span>Cancel</span>
        </button>
      </div>
    </form>
  </section>
</template>

<script>
export default {
  name: 'EmployeeEditForm',
  components: {},
  data() {
    if (this.employee) {
      return {
        id: this.employee.id,
        name: this.employee.name,
        department: this.employee.department,
        salary: this.employee.salary,
      };
    } else {
      return {
        id: null,
        name: "",
        department: "",
        salary: null,
      };
    }

  },
  props: {
    employee: Object,
    client: Object,
    departmentOptions: Array,
  },
  computed: {
    getSaveIconClass: function () {
      return `fa ${this.employee === null  ? 'fa-plus' : 'fa-check'}`;
    },
    getSaveIconText: function () {
      return `${this.employee === null  ? 'Add' : 'Save'} Employee`;
    },
  },
  methods: {
    saveEdit: function () {
      // pass all data back through the event
      this.$emit('save-edit', this.$data);
    }
  }
};
</script>
