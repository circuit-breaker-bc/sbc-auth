<template>
  <v-data-table
    id="virtual-scroll-table"
    class="base-table"
    :disable-sort="true"
    :fixed-header="height ? true : false"
    :footer-props="{ itemsPerPageOptions: [5, 10, 15, 20] }"
    :items-per-page="5"
    :headers="headers"
    :items="pageHide ? visibleItems : sortedItems"
    hide-default-header
    :height="height ? height : ''"
    :loading="filtering || loading"
    :mobile-breakpoint="0"
    :options.sync="tableDataOptions"
    :server-items-length="totalItems"
    :hide-default-footer="pageHide ? true : false"
    :expanded.sync="expanded"
  >
    <!-- Headers (two rows) -->
    <template #header>
      <thead class="base-table__header">
        <tr
          v-if="hasTitleSlot"
          class="table-title-row"
        >
          <th
            id="table-title-cell"
            :colspan="headers.length"
          >
            <slot name="header-title" />
          </th>
        </tr>
        <!-- First row has titles. -->
        <slot
          name="header-title-slot"
          :headers="headers"
        >
          <tr :style="{ 'background-color': headerBg }">
            <th
              v-for="header, i in headers"
              :key="header.col + i"
              :class="[header.class, 'base-table__header__title', `header-${header.col}`]"
              :style="header.minWidth ? { 'min-width': header.minWidth, 'max-width': header.minWidth } : {'width': header.width}"
            >
              <slot
                :name="'header-title-slot-' + header.col"
                :header="header"
              >
                <span v-html="header.value" />
              </slot>
            </th>
          </tr>
        </slot>
        <!-- Second row has filters. -->
        <slot
          v-if="!hideFilters"
          name="header-filter-slot"
          :headers="headers"
        >
          <tr :style="{ 'background-color': headerBg }">
            <th
              v-for="header in headers"
              :key="header.col"
              :class="[header.class, 'base-table__header__filter pb-5']"
            >
              <slot
                :name="'header-filter-slot-' + header.col"
                :header="header"
              />
              <HeaderFilter
                :filtering="filtering"
                :filters="filters"
                :header="header"
                :setFiltering="setFiltering"
                :sortedItems="setItems"
                :updateFilter="updateFilter"
                :headers="headers"
                :setSortedItems="setSortedItems"
              />
            </th>
          </tr>
        </slot>
      </thead>
    </template>

    <!-- Items -->
    <template #item="{ item, index }">
      <tr
        :key="index"
        :class="[
          index==highlightIndex ? highlightClass :'base-table__item-row',
          isRowExpanded ? isRowExpanded(item) : ''
        ]"
        @click="onRowClick ? onRowClick(item) : null"
      >
        <td
          v-for="header in headers"
          :key="'item-' + header.col"
          :class="[header.itemClass, 'base-table__item-cell', `cell-${header.col}`]"
        >
          <slot
            :header="header"
            :item="item"
            :index="index"
            :name="'item-slot-' + header.col"
          >
            <span
              v-if="header.itemFn"
              v-html="header.itemFn(item)"
            />
            <span v-else>{{ item[header.col] }}</span>
          </slot>
        </td>
      </tr>
    </template>
    <!-- Expandable Row details -->
    <template #expanded-item="{ headers, item }">
      <slot
        name="expanded-item"
        :headers="headers"
        :item="item"
      />
    </template>

    <template #[`body.append`]>
      <tr v-if="useObserver && !reachedEnd">
        <td :colspan="headers.length">
          <TableObserver @intersect="getNext()" />
        </td>
      </tr>
    </template>

    <!-- Loading -->
    <template #loading>
      <div
        class="py-8 base-table__text"
        v-html="loadingText"
      />
    </template>

    <!-- No data -->
    <template #no-data>
      <div
        class="py-8 base-table__text"
        v-html="noDataText"
      />
    </template>

    <!-- Override pagination text -->
    <template
      v-if="disableRowCount"
      #[`footer.page-text`]
    >
      Page {{ tableDataOptions.page }}
    </template>
  </v-data-table>
</template>

<script lang="ts">
import { PropType, defineComponent, nextTick, reactive, ref, toRefs, watch } from '@vue/composition-api'
import { BaseTableHeaderI } from './interfaces'
import { DEFAULT_DATA_OPTIONS } from './resources'
import { DataOptions } from 'vuetify'
import HeaderFilter from './components/HeaderFilter.vue'
import TableObserver from './components/TableObserver.vue'
import _ from 'lodash'

// FUTURE: remove this in vue 3 upgrade (typing will be inferred properly)
interface BaseTableStateI {
  filtering: boolean,
  headers: BaseTableHeaderI[],
  sortedItems: object[],
  tableDataOptions: DataOptions,
  visibleItems: object[],
  expanded: object[]
}

export default defineComponent({
  name: 'BaseVDataTable',
  components: { HeaderFilter, TableObserver },
  props: {
    clearFiltersTrigger: { default: 1 },
    headerBg: { default: 'white' },
    height: { type: String, default: null },
    itemKey: { type: String, required: true },
    loading: { default: false },
    loadingText: { default: 'Loading...' },
    noDataText: { default: 'No results found.' },
    setItems: { default: [] as object[] },
    setHeaders: { default: [] as BaseTableHeaderI[] },
    setTableDataOptions: { default: () => _.cloneDeep(DEFAULT_DATA_OPTIONS) as DataOptions },
    totalItems: { type: Number, required: true },
    pageHide: { default: false },
    updateFilter: { type: Function as PropType<(filterField?: string, value?: any) => void>, default: () => {} },
    filters: { default: function () { return { isActive: false, filterPayload: {} } }, required: false },
    customPagination: { default: false },
    highlightIndex: { default: -1 },
    highlightClass: { type: String, default: '' },
    hasTitleSlot: { type: Boolean },
    useObserver: { type: Boolean, required: false },
    observerCallback: { type: Function as PropType<() => Promise<boolean>>, required: false, default: null },
    hideFilters: { type: Boolean, default: false },
    setExpanded: { default: () => [] as object[] },
    disableRowCount: { type: Boolean, default: false },
    isRowExpanded: { type: Function as PropType<(item: any) => string>, default: null },
    onRowClick: { type: Function as PropType<(item: any) => void>, default: null }
  },
  emits: ['update-table-options'],
  setup (props, { emit }) {
    // reactive vars
    const state = (reactive({
      filtering: false,
      headers: _.cloneDeep(props.setHeaders),
      sortedItems: [...props.setItems],
      tableDataOptions: props.setTableDataOptions,
      visibleItems: [],
      expanded: []
    }) as unknown) as BaseTableStateI

    const currentPage = ref(1)
    const perPage = ref(50)
    const firstItem = ref(null) // first item in table
    const reachedEnd = ref(false)

    const getNext = _.debounce(async () => {
      if (props.loading) return
      if (props.observerCallback) {
        reachedEnd.value = await props.observerCallback()
      } else if (!reachedEnd.value && state.sortedItems.length > state.visibleItems.length) {
        currentPage.value++
        const start = (currentPage.value - 1) * perPage.value
        const end = start + perPage.value
        const newItems = state.sortedItems.slice(start, end)

        await nextTick()
        state.visibleItems.push(...newItems)

        if (state.sortedItems.length <= state.visibleItems.length) {
          reachedEnd.value = true
        }
      }
    }, 100) // Adjust the wait time as needed

    const scrollToTop = () => {
      const table = document.querySelector('.v-data-table__wrapper')
      if (table) table.scrollTop = 0
    }

    watch(() => state.sortedItems, () => {
      if (props.setItems && !props.observerCallback) {
        state.visibleItems = state.sortedItems.slice(0, perPage.value)
        firstItem.value = state.visibleItems[0]
        currentPage.value = 1
        reachedEnd.value = false
        scrollToTop()
      }
      if (props.observerCallback) {
        state.visibleItems = state.sortedItems
      }
    }, { immediate: true })

    const setFiltering = (filter: boolean) => {
      state.filtering = filter
      reachedEnd.value = false
    }

    const setSortedItems = (items: object[]) => {
      state.sortedItems = [...items]
    }
    watch(() => props.setExpanded, (val: object[]) => { state.expanded = [...val] }, { immediate: true })
    watch(() => props.setItems, (val: object[]) => { state.sortedItems = [...val] }, { immediate: true })
    watch(() => props.setHeaders, (val: BaseTableHeaderI[]) => {
      // maintain filters
      const filters = {}
      state.headers.forEach((header) => {
        if (header.hasFilter) filters[header.col] = header.customFilter.value
      })
      val.forEach((header) => {
        if (Object.keys(filters).includes(header.col)) header.customFilter.value = filters[header.col]
      })
      state.headers = val
    })
    watch(() => props.clearFiltersTrigger, () => {
      state.headers.forEach((header) => {
        if (header.hasFilter) header.customFilter.value = ''
      })
      state.sortedItems = props.setItems
    })

    watch(() => state.tableDataOptions, (val: DataOptions) => { emit('update-table-options', val) })

    return {
      setFiltering,
      ...toRefs(state),
      setSortedItems,
      getNext,
      reachedEnd
    }
  }
})
</script>

<style lang="scss" scoped>
.base-table {

  &__header {

    &__filter {
      padding-top: 10px;

      &__select,
      &__textbox {
        font-size: 0.825rem !important;

        .v-input__control .v-input__slot .v-input__append-inner .v-input__icon.v-input__icon--clear .v-icon {
          color: $app-blue;
        }
      }
    }

    &__title {
      color: $gray9 !important;
      font-size: 0.875rem;
    }

  }

  &__item-cell {
    border: 0px;
    font-size: 0.875rem;
    padding: 8px 0 8px 16px;
    vertical-align: top;
  }

  &__item-row-green {
    background-color: $table-green !important;
  }

  &__text {
    border: 0px;
    position: sticky;
    left: 0;
    flex-grow: 0;
    flex-shrink: 0;
  }

  .table-title-row {
    background-color: $BCgovBlue0;
  }

  ::v-deep .v-data-footer {
    min-width: 100%;
  }

  ::v-deep .v-data-table__wrapper::-webkit-scrollbar {
    width: .625rem;
    height: 0.50rem;
    overflow-x: hidden;
  }

  ::v-deep .v-data-table__wrapper::-webkit-scrollbar-track {
    overflow: auto;
  }

  ::v-deep .v-data-table__wrapper::-webkit-scrollbar-thumb {
    border-radius: 5px;
    background-color: lightgray;
  }

  ::v-deep .v-label,
  ::v-deep .v-label--active {
    color: $gray7 !important;
    font-size: .825rem;
    transform: None !important;
  }

  ::v-deep .v-label--active {
    color: $gray7;
    font-size: .825rem;
  }

  ::v-deep .v-input__slot {
    font-weight: normal;
    height: 42px !important;
    min-height: 42px !important;

    .v-select__slot label {
      top: 12px;
    }

    .v-text-field__slot input,
    .v-text-field__slot input::placeholder {
      color: $gray7;
      font-size: 0.825rem !important;
    }
  }

  ::v-deep .v-input__icon--clear .theme--light.v-icon {
    color: $app-blue;
  }

  ::v-deep .v-text-field--enclosed.v-input--dense:not(.v-text-field--solo) .v-input__append-inner {
    margin-top: 10px;
  }

  ::v-deep .v-list-item .v-list-item--link .theme--light:hover {
    background-color: $gray1;
  }

  .fixed-action-column {
    position: sticky;
    right:0;
    left:0;
    border-left: 1px solid #dee2e6;
    box-shadow: -2px 4px 4px -1px #adb5bd;
    background: white;
  }
}
</style>
